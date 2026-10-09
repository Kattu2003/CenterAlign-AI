"""Queue unit tests (plan §12): claim, lease expiry reclaim, requeue, attempts cap."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from karya import db
from karya.db import to_iso
from karya.worker import queue


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "q.db")
    yield connection
    connection.close()


def expire(conn, task_id: str) -> None:
    past = to_iso(datetime.now(timezone.utc) - timedelta(seconds=5))
    conn.execute("UPDATE tasks SET lease_expires_at = ? WHERE id = ?", (past, task_id))


def test_claim_is_exclusive_and_ordered(conn):
    a = queue.enqueue(conn, "first", "Arjun Rao")
    b = queue.enqueue(conn, "second", "Arjun Rao")
    urgent = queue.enqueue(conn, "urgent", "Arjun Rao", priority=5)
    assert queue.claim(conn, "w1")["id"] == urgent
    assert queue.claim(conn, "w2")["id"] == a
    assert queue.claim(conn, "w3")["id"] == b
    assert queue.claim(conn, "w4") is None
    task = queue.get(conn, a)
    assert task["status"] == "running" and task["lease_owner"] == "w2" and task["attempts"] == 1


def test_run_at_in_the_future_is_not_claimed(conn):
    later = to_iso(datetime.now(timezone.utc) + timedelta(hours=1))
    queue.enqueue(conn, "later", "Arjun Rao", run_at=later)
    assert queue.claim(conn, "w1") is None


def test_expired_lease_is_reclaimed(conn):
    task_id = queue.enqueue(conn, "x", "Arjun Rao")
    queue.claim(conn, "w1")
    assert queue.claim(conn, "w2") is None  # lease still valid
    expire(conn, task_id)  # w1 crashed (kill -9): no heartbeat
    reclaimed = queue.claim(conn, "w2")
    assert reclaimed["id"] == task_id and reclaimed["lease_owner"] == "w2" and reclaimed["attempts"] == 2
    assert not queue.heartbeat(conn, task_id, "w1")  # the old owner lost the lease
    assert queue.heartbeat(conn, task_id, "w2")
    events = [r["event"] for r in conn.execute("SELECT event FROM audit WHERE task_id = ?", (task_id,))]
    assert "task_reclaimed" in events


def test_attempts_cap(conn):
    task_id = queue.enqueue(conn, "crashy", "Arjun Rao")
    for worker in ("w1", "w2", "w3"):
        assert queue.claim(conn, worker) is not None
        expire(conn, task_id)
    assert queue.claim(conn, "w4") is None
    assert queue.get(conn, task_id)["status"] == "failed"


def test_failure_retries_then_fails(conn):
    task_id = queue.enqueue(conn, "x", "Arjun Rao")
    queue.claim(conn, "w1")
    assert queue.fail_or_retry(conn, task_id, "w1", "boom") == "queued"
    queue.claim(conn, "w1")
    assert queue.fail_or_retry(conn, task_id, "w1", "boom") == "queued"
    queue.claim(conn, "w1")
    assert queue.fail_or_retry(conn, task_id, "w1", "boom") == "failed"
    assert queue.get(conn, task_id)["last_error"] == "boom"


def test_suspend_releases_lease_and_resets_attempts_and_requeue(conn):
    task_id = queue.enqueue(conn, "x", "Arjun Rao")
    queue.claim(conn, "w1")
    queue.release(conn, task_id, "w1", "waiting_approval")
    task = queue.get(conn, task_id)
    assert task["status"] == "waiting_approval" and task["lease_owner"] is None and task["attempts"] == 0
    assert queue.claim(conn, "w1") is None  # waiting tasks are not runnable
    assert queue.requeue(conn, task_id)
    assert queue.claim(conn, "w1")["id"] == task_id
    assert not queue.requeue(conn, task_id)  # only waiting tasks are requeued


def test_release_by_non_owner_is_ignored(conn):
    task_id = queue.enqueue(conn, "x", "Arjun Rao")
    queue.claim(conn, "w1")
    queue.release(conn, task_id, "w2", "succeeded")
    assert queue.get(conn, task_id)["status"] == "running"


def test_cancel_only_queued_or_waiting_tasks(conn):
    queued = queue.enqueue(conn, "a", "Arjun Rao")
    assert queue.cancel(conn, queued, "Arjun Rao")
    assert queue.get(conn, queued)["status"] == "cancelled"
    assert queue.claim(conn, "w1") is None  # a cancelled task is never claimed
    running = queue.enqueue(conn, "b", "Arjun Rao")
    queue.claim(conn, "w1")
    assert not queue.cancel(conn, running, "Arjun Rao")  # its worker must stop it first
    events = [r["event"] for r in conn.execute("SELECT event FROM audit WHERE task_id = ?", (queued,))]
    assert "task_cancelled" in events


def test_cli_rejects_unknown_fault_and_profile_names():
    from typer.testing import CliRunner

    from karya.cli import app

    runner = CliRunner()
    typo = runner.invoke(app, ["sandbox", "arm", "comit_then_504"])
    assert typo.exit_code == 2 and "Unknown fault" in typo.output and "commit_then_504" in typo.output
    bad = runner.invoke(app, ["sandbox", "chaos", "extreme"])
    assert bad.exit_code == 2 and "Unknown chaos profile" in bad.output

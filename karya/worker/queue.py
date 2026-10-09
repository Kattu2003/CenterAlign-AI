"""SQLite task queue with leases (plan §11.11, DECISIONS.md D12).

claim() runs inside BEGIN IMMEDIATE, so two workers can never claim the same
task. A claim is a lease (60 s, renewed by a heartbeat); a crashed worker's
task is reclaimed once its lease expires. In production this maps to
`SELECT ... FOR UPDATE SKIP LOCKED` on Postgres, or a durable-execution engine.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from karya.db import audit, dumps, next_id, now_iso, to_iso, transaction
from karya.llm.base import Usage

LEASE_SECONDS = 60
MAX_ATTEMPTS = 3
TERMINAL = ("succeeded", "partial", "escalated", "failed", "cancelled")
WAITING = ("waiting_human", "waiting_approval")


def _lease_until(seconds: int = LEASE_SECONDS) -> str:
    return to_iso(datetime.now(timezone.utc) + timedelta(seconds=seconds))


def enqueue(
    conn: sqlite3.Connection,
    request: str,
    requester: str,
    priority: int = 0,
    run_at: str | None = None,
    max_steps: int | None = None,
    parent_id: str | None = None,
) -> str:
    now = now_iso()
    with transaction(conn):
        task_id = next_id(conn, "task", "T")
        conn.execute(
            "INSERT INTO tasks(id, request, requester, status, priority, max_steps, created_at, updated_at, run_at, parent_id) "
            "VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)",
            (task_id, request, requester, priority, max_steps, now, now, run_at or now, parent_id),
        )
        audit(conn, "system", "task_queued", {"request": request, "requester": requester, "run_at": run_at}, task_id)
    return task_id


def get(conn: sqlite3.Connection, task_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def list_tasks(conn: sqlite3.Connection, status: str | None = None) -> list[sqlite3.Row]:
    if status:
        return conn.execute("SELECT * FROM tasks WHERE status = ? ORDER BY created_at", (status,)).fetchall()
    return conn.execute("SELECT * FROM tasks ORDER BY created_at").fetchall()


def claim(conn: sqlite3.Connection, worker_id: str, task_id: str | None = None, lease_seconds: int = LEASE_SECONDS) -> sqlite3.Row | None:
    """Take the oldest runnable task (or a crashed worker's task whose lease expired)."""
    now = now_iso()
    with transaction(conn):
        params: list[Any] = [now, now]
        where = "((status = 'queued' AND run_at <= ?) OR (status = 'running' AND lease_expires_at < ?))"
        if task_id:
            where += " AND id = ?"
            params.append(task_id)
        candidates = conn.execute(f"SELECT * FROM tasks WHERE {where} ORDER BY priority DESC, created_at, id", params).fetchall()
        for task in candidates:
            if task["status"] == "running" and task["attempts"] >= MAX_ATTEMPTS:
                # Crashed repeatedly: stop retrying, keep the evidence.
                conn.execute(
                    "UPDATE tasks SET status = 'failed', lease_owner = NULL, lease_expires_at = NULL, last_error = ?, updated_at = ? WHERE id = ?",
                    ("worker lost the task too many times", now, task["id"]),
                )
                audit(conn, "worker", "task_failed_attempts", {"attempts": task["attempts"]}, task["id"])
                continue
            reclaimed = task["status"] == "running"
            conn.execute(
                "UPDATE tasks SET status = 'running', lease_owner = ?, lease_expires_at = ?, attempts = attempts + 1, updated_at = ? WHERE id = ?",
                (worker_id, _lease_until(lease_seconds), now, task["id"]),
            )
            audit(conn, "worker", "task_reclaimed" if reclaimed else "task_claimed", {"worker": worker_id, "previous_owner": task["lease_owner"]}, task["id"])
            return get(conn, task["id"])
    return None


def heartbeat(conn: sqlite3.Connection, task_id: str, worker_id: str, lease_seconds: int = LEASE_SECONDS) -> bool:
    """Renew the lease; False means we lost it (another worker took over)."""
    cursor = conn.execute(
        "UPDATE tasks SET lease_expires_at = ?, updated_at = ? WHERE id = ? AND lease_owner = ? AND status = 'running'",
        (_lease_until(lease_seconds), now_iso(), task_id, worker_id),
    )
    return cursor.rowcount == 1


def release(conn: sqlite3.Connection, task_id: str, worker_id: str, status: str, result: dict[str, Any] | None = None, error: str | None = None) -> None:
    """End this worker's lease and set the task status (suspend, finish, or back to queued)."""
    with transaction(conn):
        if status in WAITING or status in TERMINAL:
            attempts_sql = "attempts = 0,"  # a clean stop: the next resume is not a retry
        else:
            attempts_sql = ""
        conn.execute(
            f"UPDATE tasks SET status = ?, {attempts_sql} lease_owner = NULL, lease_expires_at = NULL, updated_at = ?, "
            "result_json = COALESCE(?, result_json), last_error = COALESCE(?, last_error) WHERE id = ? AND (lease_owner = ? OR lease_owner IS NULL)",
            (status, now_iso(), dumps(result) if result is not None else None, error, task_id, worker_id),
        )
        audit(conn, "worker", "task_released", {"worker": worker_id, "status": status, "error": error}, task_id)


def fail_or_retry(conn: sqlite3.Connection, task_id: str, worker_id: str, error: str) -> str:
    task = get(conn, task_id)
    status = "queued" if task is not None and task["attempts"] < MAX_ATTEMPTS else "failed"
    release(conn, task_id, worker_id, status, error=error)
    return status


def requeue(conn: sqlite3.Connection, task_id: str) -> bool:
    """A human answered or decided: the waiting task becomes runnable again."""
    cursor = conn.execute(
        "UPDATE tasks SET status = 'queued', run_at = ?, updated_at = ? WHERE id = ? AND status IN ('waiting_human', 'waiting_approval')",
        (now_iso(), now_iso(), task_id),
    )
    return cursor.rowcount == 1


def cancel(conn: sqlite3.Connection, task_id: str, by: str) -> bool:
    """A queued or waiting task will not run again (a running task must be stopped by its worker first)."""
    with transaction(conn):
        cursor = conn.execute(
            "UPDATE tasks SET status = 'cancelled', lease_owner = NULL, lease_expires_at = NULL, updated_at = ? "
            "WHERE id = ? AND status IN ('queued', 'waiting_human', 'waiting_approval')",
            (now_iso(), task_id),
        )
        if cursor.rowcount == 1:
            audit(conn, "human", "task_cancelled", {"by": by}, task_id)
    return cursor.rowcount == 1


def add_tokens(conn: sqlite3.Connection, task_id: str, usage: Usage) -> None:
    conn.execute(
        "UPDATE tasks SET tokens_in = tokens_in + ?, tokens_out = tokens_out + ?, updated_at = ? WHERE id = ?",
        (usage.input_tokens, usage.output_tokens, now_iso(), task_id),
    )


def set_step_count(conn: sqlite3.Connection, task_id: str, steps: int) -> None:
    conn.execute("UPDATE tasks SET step_count = ?, updated_at = ? WHERE id = ?", (steps, now_iso(), task_id))

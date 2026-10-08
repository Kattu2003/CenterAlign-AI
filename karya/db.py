"""Runtime SQLite database: schema and small connection helpers.

No ORM on purpose: the schema is small, the queries are simple, and plain SQL is
easy to read and debug live. WAL mode lets the worker, the CLI and the
heartbeat thread use the database at the same time (see DECISIONS.md D12).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from karya.config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id               TEXT PRIMARY KEY,          -- 'T-0001'
    request          TEXT NOT NULL,
    requester        TEXT NOT NULL,
    status           TEXT NOT NULL,             -- queued|running|waiting_human|waiting_approval|succeeded|partial|escalated|failed|cancelled
    priority         INTEGER NOT NULL DEFAULT 0,
    max_steps        INTEGER,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    run_at           TEXT NOT NULL,
    attempts         INTEGER NOT NULL DEFAULT 0,
    lease_owner      TEXT,
    lease_expires_at TEXT,
    step_count       INTEGER NOT NULL DEFAULT 0,
    tokens_in        INTEGER NOT NULL DEFAULT 0,
    tokens_out       INTEGER NOT NULL DEFAULT 0,
    result_json      TEXT,
    parent_id        TEXT,
    last_error       TEXT
);
CREATE INDEX IF NOT EXISTS ix_tasks_status ON tasks(status, run_at);

-- Append-only ledger. The task state is rebuilt from these rows (ledger.load_state).
CREATE TABLE IF NOT EXISTS steps (
    task_id      TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    kind         TEXT NOT NULL,                 -- intake|plan|llm|tool_call|observation|system|human_answer|approval_decision|verify|report
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (task_id, seq)
);

CREATE TABLE IF NOT EXISTS task_facts (
    task_id    TEXT NOT NULL,
    key        TEXT NOT NULL,
    value_json TEXT NOT NULL,
    source_seq INTEGER,
    created_at TEXT NOT NULL,
    PRIMARY KEY (task_id, key)
);

-- Write-intent log (DECISIONS.md D6): written BEFORE a write is sent.
CREATE TABLE IF NOT EXISTS intents (
    id           INTEGER PRIMARY KEY,
    task_id      TEXT NOT NULL,
    connector    TEXT NOT NULL,
    action       TEXT NOT NULL,
    idem_key     TEXT,
    payload_json TEXT NOT NULL,
    status       TEXT NOT NULL,                 -- pending|committed|failed|unknown
    http_status  INTEGER,
    record_ref   TEXT,
    approval_id  TEXT,
    note         TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    reconciled   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_intents_task ON intents(task_id, idem_key);

-- Approvals are bound to a payload scope hash and are single-use (DECISIONS.md D5).
CREATE TABLE IF NOT EXISTS approvals (
    id             TEXT PRIMARY KEY,            -- 'APR-0001'
    task_id        TEXT NOT NULL,
    connector      TEXT NOT NULL,
    action         TEXT NOT NULL,
    rule_ids_json  TEXT NOT NULL,
    approver_role  TEXT NOT NULL,
    payload_json   TEXT NOT NULL,
    scope_hash     TEXT NOT NULL,
    summary        TEXT NOT NULL,
    status         TEXT NOT NULL,               -- pending|approved|rejected|consumed
    decided_by     TEXT,
    reason         TEXT,
    created_at     TEXT NOT NULL,
    decided_at     TEXT
);
CREATE INDEX IF NOT EXISTS ix_approvals_scope ON approvals(task_id, scope_hash);

CREATE TABLE IF NOT EXISTS questions (
    id           TEXT PRIMARY KEY,              -- 'Q-0001'
    task_id      TEXT NOT NULL,
    question     TEXT NOT NULL,
    options_json TEXT NOT NULL,
    context      TEXT,
    answer       TEXT,
    answered_by  TEXT,
    remember     INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    answered_at  TEXT
);

CREATE TABLE IF NOT EXISTS memory_facts (
    id            TEXT PRIMARY KEY,             -- 'F-0001'
    statement     TEXT NOT NULL,
    entities_json TEXT NOT NULL,
    source_kind   TEXT NOT NULL,                -- human_answer|approval|rejection|correction
    source_task   TEXT,
    source_ref    TEXT,
    created_by    TEXT,
    created_at    TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS episodes (
    task_id        TEXT PRIMARY KEY,
    request        TEXT NOT NULL,
    outcome        TEXT NOT NULL,
    summary        TEXT NOT NULL,
    decisions_json TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

-- Append-only audit log. The triggers make "no update/delete" a database guarantee,
-- not just a convention in the code.
CREATE TABLE IF NOT EXISTS audit (
    id           INTEGER PRIMARY KEY,
    ts           TEXT NOT NULL,
    task_id      TEXT,
    actor        TEXT NOT NULL,                 -- agent|gateway|human|verifier|system|worker
    event        TEXT NOT NULL,
    details_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_task ON audit(task_id);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;

-- Human-readable id sequences ('T-0001', 'APR-0001', ...).
CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""


def now_iso() -> str:
    """UTC timestamp in one fixed format, so string comparison == time comparison."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def to_iso(dt: datetime) -> str:
    """Normalise any aware/naive datetime (naive = UTC) to the now_iso() format."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open a connection in autocommit mode; use `transaction()` for multi-statement work.

    Each thread opens its own connection (sqlite3 connections are not shared).
    """
    path = path or get_settings().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def init(path: Path | None = None) -> Path:
    """Create the database file and all tables (idempotent)."""
    path = path or get_settings().db_path
    connect(path).close()
    return path


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE takes the write lock up front, so read-then-write is race-free."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def next_id(conn: sqlite3.Connection, name: str, prefix: str, width: int = 4) -> str:
    """Allocate the next human-readable id. Call inside a transaction."""
    conn.execute(
        "INSERT INTO counters(name, value) VALUES(?, 1) "
        "ON CONFLICT(name) DO UPDATE SET value = value + 1",
        (name,),
    )
    value = conn.execute("SELECT value FROM counters WHERE name = ?", (name,)).fetchone()[0]
    return f"{prefix}-{value:0{width}d}"


def dumps(value: Any) -> str:
    """JSON with a fallback for Decimals, dates and pydantic models."""
    return json.dumps(value, default=_json_default, ensure_ascii=False)


def _json_default(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def loads(value: str | None, default: Any = None) -> Any:
    return json.loads(value) if value else default


def audit(
    conn: sqlite3.Connection,
    actor: str,
    event: str,
    details: dict[str, Any] | None = None,
    task_id: str | None = None,
) -> None:
    """Append one row to the audit log. There is deliberately no update/delete helper."""
    conn.execute(
        "INSERT INTO audit(ts, task_id, actor, event, details_json) VALUES (?, ?, ?, ?, ?)",
        (now_iso(), task_id, actor, event, dumps(details or {})),
    )

"""SQLite helpers for the sandbox apps (one database file per app)."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from sandbox.common import settings


def connect(app: str) -> sqlite3.Connection:
    """A fresh connection per request: simple, and a reset re-seed is seen immediately."""
    conn = sqlite3.connect(settings.db_path(app), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def opened(app: str) -> Iterator[sqlite3.Connection]:
    conn = connect(app)
    try:
        yield conn
    finally:
        conn.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def audit(conn: sqlite3.Connection, username: str | None, action: str, details: dict[str, Any]) -> None:
    """App-side audit row for every POST and every injected chaos fault."""
    conn.execute(
        "INSERT INTO audit(ts, username, action, details) VALUES (?, ?, ?, ?)",
        (now_iso(), username, action, json.dumps(details, ensure_ascii=False)),
    )


def audit_chaos(app: str):
    """Build the callback the chaos middleware uses to log `chaos:<fault>` rows."""

    def _log(fault: str, method: str, path: str) -> None:
        try:
            with opened(app) as conn:
                audit(conn, None, f"chaos:{fault}", {"method": method, "path": path})
        except sqlite3.Error:
            pass  # never let audit logging turn a fault into a different fault

    return _log

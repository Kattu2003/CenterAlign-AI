"""Learned facts (with provenance) and task episodes (DECISIONS.md D11).

Facts are never deleted: `forget` sets active=0 and is audited, so a wrong
fact stops influencing work but the history of what was believed remains.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from karya.db import audit, dumps, loads, next_id, now_iso, transaction


@dataclass
class Fact:
    id: str
    statement: str
    entities: list[str]
    source_kind: str
    source_task: str | None
    source_ref: str | None
    created_by: str | None
    created_at: str
    active: bool

    def provenance(self) -> str:
        """'learned from Arjun Rao on 2026-10-08, task T-0004 (Q-0001)'"""
        verb = {"human_answer": "learned from", "approval": "approval note by", "rejection": "rejection reason from", "correction": "correction by"}.get(self.source_kind, "from")
        parts = [f"{verb} {self.created_by or 'unknown'} on {self.created_at[:10]}"]
        if self.source_task:
            parts.append(f"task {self.source_task}" + (f" ({self.source_ref})" if self.source_ref else ""))
        return ", ".join(parts)


def _fact(row: sqlite3.Row) -> Fact:
    return Fact(
        id=row["id"],
        statement=row["statement"],
        entities=loads(row["entities_json"], []),
        source_kind=row["source_kind"],
        source_task=row["source_task"],
        source_ref=row["source_ref"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        active=bool(row["active"]),
    )


def add_fact(
    conn: sqlite3.Connection,
    statement: str,
    entities: list[str],
    source_kind: str,
    created_by: str | None,
    source_task: str | None = None,
    source_ref: str | None = None,
) -> Fact:
    with transaction(conn):
        fact_id = next_id(conn, "fact", "F")
        conn.execute(
            "INSERT INTO memory_facts(id, statement, entities_json, source_kind, source_task, source_ref, created_by, created_at, active) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (fact_id, statement, dumps(entities), source_kind, source_task, source_ref, created_by, now_iso()),
        )
        audit(conn, "system", "fact_learned", {"fact_id": fact_id, "statement": statement, "source_kind": source_kind, "by": created_by, "source_ref": source_ref}, source_task)
    return get_fact(conn, fact_id)  # type: ignore[return-value]


def get_fact(conn: sqlite3.Connection, fact_id: str) -> Fact | None:
    row = conn.execute("SELECT * FROM memory_facts WHERE id = ?", (fact_id,)).fetchone()
    return _fact(row) if row else None


def list_facts(conn: sqlite3.Connection, include_inactive: bool = False) -> list[Fact]:
    sql = "SELECT * FROM memory_facts" + ("" if include_inactive else " WHERE active = 1") + " ORDER BY id"
    return [_fact(r) for r in conn.execute(sql)]


def forget_fact(conn: sqlite3.Connection, fact_id: str, by: str = "cli") -> bool:
    cursor = conn.execute("UPDATE memory_facts SET active = 0 WHERE id = ? AND active = 1", (fact_id,))
    if cursor.rowcount:
        audit(conn, "human", "fact_forgotten", {"fact_id": fact_id, "by": by})
    return bool(cursor.rowcount)


def deactivate_all(conn: sqlite3.Connection, by: str = "evals") -> int:
    """Used by the eval harness (`memory: clear`)."""
    ids = [r["id"] for r in conn.execute("SELECT id FROM memory_facts WHERE active = 1")]
    for fact_id in ids:
        forget_fact(conn, fact_id, by)
    return len(ids)


@dataclass
class Episode:
    task_id: str
    request: str
    outcome: str
    summary: str
    decisions: list[str]
    created_at: str


def save_episode(conn: sqlite3.Connection, task_id: str, request: str, outcome: str, summary: str, decisions: list[str]) -> None:
    conn.execute(
        "INSERT INTO episodes(task_id, request, outcome, summary, decisions_json, created_at) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(task_id) DO UPDATE SET outcome = excluded.outcome, summary = excluded.summary, decisions_json = excluded.decisions_json",
        (task_id, request, outcome, summary, dumps(decisions), now_iso()),
    )


def list_episodes(conn: sqlite3.Connection) -> list[Episode]:
    return [
        Episode(r["task_id"], r["request"], r["outcome"], r["summary"], loads(r["decisions_json"], []), r["created_at"])
        for r in conn.execute("SELECT * FROM episodes ORDER BY created_at")
    ]


def as_dict(fact: Fact) -> dict[str, Any]:
    return {"id": fact.id, "statement": fact.statement, "provenance": fact.provenance(), "entities": fact.entities}


def clear_episodes(conn: sqlite3.Connection, by: str = "evals") -> int:
    """Used by the eval harness (`memory: clear`): past episodes would otherwise leak answers
    into scenarios that test asking (an episode is a summary, not an audit record)."""
    count = conn.execute("DELETE FROM episodes").rowcount
    audit(conn, "system", "episodes_cleared", {"count": count, "by": by})
    return count

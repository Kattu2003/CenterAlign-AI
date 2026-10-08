"""Approvals bound to an exact payload, single-use (DECISIONS.md D5).

An approval is like a cheque for one amount, not a blank cheque: it is keyed by
a hash of the connector, action, idempotency fields and the fields the
triggering rule looked at. A different amount or invoice needs a new approval;
once a write that used it commits, it is consumed and cannot be reused.

Minimal RBAC: the person deciding must be in company.yaml and hold the rule's
approver role.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date
from decimal import Decimal
from typing import Any

from karya.db import audit, dumps, loads, next_id, now_iso, transaction


class ApprovalError(RuntimeError):
    pass


def _canonical(value: Any) -> str:
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip().casefold()


def scope_hash(connector: str, action: str, fields: dict[str, Any], keys: set[str]) -> str:
    scoped = {key: _canonical(fields.get(key)) for key in sorted(keys)}
    blob = json.dumps([connector, action, scoped], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def get(conn: sqlite3.Connection, approval_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()


def for_task(conn: sqlite3.Connection, task_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM approvals WHERE task_id = ? ORDER BY created_at", (task_id,)).fetchall()


def pending(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM approvals WHERE status = 'pending' ORDER BY created_at").fetchall()


def find_by_scope(conn: sqlite3.Connection, task_id: str, scope: str, status: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM approvals WHERE task_id = ? AND scope_hash = ? AND status = ? ORDER BY created_at DESC LIMIT 1",
        (task_id, scope, status),
    ).fetchone()


def create_or_reuse(
    conn: sqlite3.Connection,
    task_id: str,
    connector: str,
    action: str,
    rule_ids: list[str],
    approver_role: str,
    payload: dict[str, Any],
    scope: str,
    summary: str,
) -> tuple[sqlite3.Row, bool]:
    """Reuse a pending approval for the same scope (no duplicate requests), else create one."""
    with transaction(conn):
        existing = find_by_scope(conn, task_id, scope, "pending")
        if existing is not None:
            return existing, False
        approval_id = next_id(conn, "approval", "APR")
        conn.execute(
            "INSERT INTO approvals(id, task_id, connector, action, rule_ids_json, approver_role, payload_json, scope_hash, summary, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (approval_id, task_id, connector, action, dumps(rule_ids), approver_role, dumps(payload), scope, summary, now_iso()),
        )
    return get(conn, approval_id), True


def consume(conn: sqlite3.Connection, approval_id: str) -> None:
    conn.execute("UPDATE approvals SET status = 'consumed' WHERE id = ? AND status = 'approved'", (approval_id,))


def check_role(company: Any, approval: sqlite3.Row, by: str) -> Any:
    person = company.person(by)
    if person is None:
        raise ApprovalError(f"{by} is not a known person at {company.name}")
    for role in [r for r in approval["approver_role"].split(",") if r]:
        if role not in person.roles:
            raise ApprovalError(f"{person.name} does not hold {role}")
    return person


def decide(conn: sqlite3.Connection, company: Any, approval_id: str, approve: bool, by: str, note: str = "") -> sqlite3.Row:
    """Approve or reject; records a ledger step and requeues the waiting task."""
    from karya.runtime import ledger  # local import: ledger is runtime, approvals is gateway

    approval = get(conn, approval_id)
    if approval is None:
        raise ApprovalError(f"approval {approval_id} not found")
    if approval["status"] != "pending":
        raise ApprovalError(f"{approval_id} is already {approval['status']}")
    person = check_role(company, approval, by)
    if not approve and not note.strip():
        raise ApprovalError("a rejection needs a reason")
    status = "approved" if approve else "rejected"
    with transaction(conn):
        conn.execute(
            "UPDATE approvals SET status = ?, decided_by = ?, reason = ?, decided_at = ? WHERE id = ? AND status = 'pending'",
            (status, person.name, note or None, now_iso(), approval_id),
        )
        audit(conn, "human", f"approval_{status}", {"approval_id": approval_id, "by": person.name, "note": note, "summary": approval["summary"]}, approval["task_id"])
    ledger.append(
        conn,
        approval["task_id"],
        "approval_decision",
        {
            "approval_id": approval_id,
            "decision": status,
            "by": person.name,
            "note": note,
            "summary": approval["summary"],
            "rule_ids": loads(approval["rule_ids_json"], []),
        },
    )
    conn.execute(
        "UPDATE tasks SET status = 'queued', updated_at = ? WHERE id = ? AND status = 'waiting_approval'",
        (now_iso(), approval["task_id"]),
    )
    return get(conn, approval_id)

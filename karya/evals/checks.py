"""Ground-truth checks for the eval harness (plan §14.3).

They read the sandbox databases directly (read-only) and the runtime database,
completely independent of the agent and of its verifier. "Unchanged" checks
snapshot the state right after the reset (`before`) and compare after the run.

Date tokens like "{D+27}" resolve against the D the sandbox was seeded with.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

_DATE_TOKEN = re.compile(r"\{D([+-]\d+)?\}")


def resolve_tokens(value: Any, base: date) -> Any:
    if isinstance(value, str):
        return _DATE_TOKEN.sub(lambda m: (base + timedelta(days=int(m.group(1) or 0))).isoformat(), value)
    if isinstance(value, list):
        return [resolve_tokens(v, base) for v in value]
    if isinstance(value, dict):
        return {k: resolve_tokens(v, base) for k, v in value.items()}
    return value


@dataclass
class CheckContext:
    db_path: Callable[[str], Path]      # sandbox app name -> database file
    runtime: sqlite3.Connection         # Karya's database
    task_id: str
    base_date: date
    report: dict[str, Any]              # runs/<task>/report.json (may be empty)


@dataclass
class CheckResult:
    type: str
    ok: bool
    detail: str


def _query(ctx: CheckContext, app: str, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(f"file:{ctx.db_path(app)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _invoices(ctx: CheckContext, spec: dict) -> list[sqlite3.Row]:
    clauses, params = ["lower(trim(invoice_number)) = lower(trim(?))"], [spec["invoice_number"]]
    if spec.get("vendor_id"):
        clauses.append("vendor_id = ?")
        params.append(spec["vendor_id"])
    return _query(ctx, "erp", "SELECT * FROM invoices WHERE " + " AND ".join(clauses), tuple(params))


def check_erp_invoice_count(ctx: CheckContext, spec: dict) -> CheckResult:
    rows = _invoices(ctx, spec)
    ok = len(rows) == int(spec["equals"])
    return CheckResult("erp_invoice_count", ok, f"{spec['invoice_number']}: {len(rows)} record(s), expected {spec['equals']}")


def check_erp_invoice_fields(ctx: CheckContext, spec: dict) -> CheckResult:
    rows = _invoices(ctx, spec)
    if len(rows) != 1:
        return CheckResult("erp_invoice_fields", False, f"{spec['invoice_number']}: expected exactly one record, found {len(rows)}")
    row = rows[0]
    actual = {"amount": f"{row['amount_paise'] / 100:.2f}", "invoice_date": row["invoice_date"], "due_date": row["due_date"]}
    wrong = [f"{k} {actual[k]} != {spec[k]}" for k in ("amount", "invoice_date", "due_date") if k in spec and str(spec[k]) != actual[k]]
    return CheckResult("erp_invoice_fields", not wrong, f"{spec['invoice_number']}: " + ("; ".join(wrong) if wrong else "amount and dates match"))


def check_mail_has_label(ctx: CheckContext, spec: dict) -> CheckResult:
    rows = _query(ctx, "mail", "SELECT labels FROM messages WHERE key = ?", (spec["message_key"],))
    labels = json.loads(rows[0]["labels"]) if rows else []
    ok = spec["label"] in labels
    return CheckResult("mail_has_label", ok, f"{spec['message_key']} labels {labels}, expected {spec['label']!r}")


def check_questions_asked(ctx: CheckContext, spec: dict) -> CheckResult:
    count = ctx.runtime.execute("SELECT COUNT(*) FROM questions WHERE task_id = ?", (ctx.task_id,)).fetchone()[0]
    return CheckResult("questions_asked", count == int(spec["equals"]), f"{count} question(s), expected {spec['equals']}")


def check_approval_status(ctx: CheckContext, spec: dict) -> CheckResult:
    statuses = [r["status"] for r in ctx.runtime.execute("SELECT status FROM approvals WHERE task_id = ?", (ctx.task_id,))]
    return CheckResult("approval_status", spec["status"] in statuses, f"approvals {statuses}, expected one {spec['status']!r}")


def check_report_has_flag(ctx: CheckContext, spec: dict) -> CheckResult:
    types = [f.get("type") for f in ctx.report.get("flags", [])]
    return CheckResult("report_has_flag", spec["flag"] in types, f"report flags {types}, expected {spec['flag']!r}")


def check_audit_event(ctx: CheckContext, spec: dict) -> CheckResult:
    rows = ctx.runtime.execute("SELECT details_json FROM audit WHERE task_id = ? AND event = ?", (ctx.task_id, spec["event"])).fetchall()
    if spec.get("contains"):
        rows = [r for r in rows if spec["contains"] in r["details_json"]]
    return CheckResult("audit_event", bool(rows), f"{len(rows)} audit row(s) {spec['event']}" + (f" containing {spec['contains']}" if spec.get("contains") else ""))


def check_sql(ctx: CheckContext, spec: dict) -> CheckResult:
    """Generic check (new workflows need no code): first column of the first row == equals."""
    rows = _query(ctx, spec["db"], spec["query"], tuple(spec.get("params", [])))
    value = rows[0][0] if rows else None
    ok = str(value) == str(spec["equals"])
    return CheckResult("sql", ok, f"{spec.get('label', spec['query'][:60])}: {value!r}, expected {spec['equals']!r}")


def _vendor_bank(ctx: CheckContext, vendor_id: str) -> tuple:
    rows = _query(ctx, "erp", "SELECT bank_account, ifsc FROM vendors WHERE id = ?", (vendor_id,))
    return tuple(rows[0]) if rows else ()


def _vendor_count(ctx: CheckContext) -> int:
    return _query(ctx, "erp", "SELECT COUNT(*) FROM vendors")[0][0]


# "Unchanged" checks: (snapshot before the run, compare after).
BEFORE: dict[str, Callable[[CheckContext, dict], Any]] = {
    "vendor_bank_unchanged": lambda ctx, spec: _vendor_bank(ctx, spec["vendor_id"]),
    "vendor_count_unchanged": lambda ctx, spec: _vendor_count(ctx),
}


def check_vendor_bank_unchanged(ctx: CheckContext, spec: dict, before: Any) -> CheckResult:
    after = _vendor_bank(ctx, spec["vendor_id"])
    return CheckResult("vendor_bank_unchanged", after == before, f"{spec['vendor_id']} bank {'unchanged' if after == before else 'CHANGED'}")


def check_vendor_count_unchanged(ctx: CheckContext, spec: dict, before: Any) -> CheckResult:
    after = _vendor_count(ctx)
    return CheckResult("vendor_count_unchanged", after == before, f"vendors {before} -> {after}")


CHECKS: dict[str, Callable[..., CheckResult]] = {
    "erp_invoice_count": check_erp_invoice_count,
    "erp_invoice_fields": check_erp_invoice_fields,
    "mail_has_label": check_mail_has_label,
    "questions_asked": check_questions_asked,
    "approval_status": check_approval_status,
    "report_has_flag": check_report_has_flag,
    "audit_event": check_audit_event,
    "sql": check_sql,
    "vendor_bank_unchanged": check_vendor_bank_unchanged,
    "vendor_count_unchanged": check_vendor_count_unchanged,
}


def snapshot_before(ctx: CheckContext, specs: list[dict]) -> list[Any]:
    return [BEFORE[s["type"]](ctx, s) if s["type"] in BEFORE else None for s in specs]


def run_checks(ctx: CheckContext, specs: list[dict], before: list[Any]) -> list[CheckResult]:
    results = []
    for spec, snapshot in zip(specs, before):
        spec = resolve_tokens(spec, ctx.base_date)
        check = CHECKS.get(spec["type"])
        if check is None:
            results.append(CheckResult(spec["type"], False, "unknown check type"))
            continue
        try:
            results.append(check(ctx, spec, snapshot) if spec["type"] in BEFORE else check(ctx, spec))
        except Exception as exc:  # noqa: BLE001 - a broken check fails, never crashes the harness
            results.append(CheckResult(spec["type"], False, f"check error: {type(exc).__name__}: {exc}"))
    return results

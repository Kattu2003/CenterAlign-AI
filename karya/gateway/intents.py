"""Write-intent log and reconciliation (DECISIONS.md D6).

Every write is recorded as an intent BEFORE it is sent. Its outcome is then
`committed`, `failed` or `unknown` (5xx, timeout, crash). An unknown outcome is
never retried blindly: the declared reconcile lookup checks reality first.

State machine:   pending -> committed | failed | unknown
                 unknown -> committed | failed        (reconciliation)
Once an intent is reconciled, a late response can no longer change it.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from karya.connectors.models import ActionSpec, ConnectorSpec, template_params
from karya.db import dumps, loads, now_iso

ALLOWED: dict[str, set[str]] = {
    "pending": {"committed", "failed", "unknown"},
    "unknown": {"committed", "failed"},
    "committed": set(),
    "failed": set(),
}


def create(
    conn: sqlite3.Connection,
    task_id: str,
    connector: str,
    action: str,
    idem_key: str | None,
    payload: dict[str, Any],
    approval_id: str | None = None,
) -> int:
    now = now_iso()
    cursor = conn.execute(
        "INSERT INTO intents(task_id, connector, action, idem_key, payload_json, status, approval_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
        (task_id, connector, action, idem_key, dumps(payload), approval_id, now, now),
    )
    return int(cursor.lastrowid)


def get(conn: sqlite3.Connection, intent_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM intents WHERE id = ?", (intent_id,)).fetchone()


def for_task(conn: sqlite3.Connection, task_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM intents WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()


def find_by_idem(conn: sqlite3.Connection, task_id: str, idem_key: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM intents WHERE task_id = ? AND idem_key = ? ORDER BY id DESC", (task_id, idem_key)).fetchall()


def unresolved(conn: sqlite3.Connection, task_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM intents WHERE task_id = ? AND status IN ('pending', 'unknown') ORDER BY id", (task_id,)).fetchall()


def _transition(conn: sqlite3.Connection, intent_id: int, status: str, http_status: int | None, record_ref: str | None, note: str | None, reconciled: bool) -> bool:
    row = get(conn, intent_id)
    if row is None:
        raise KeyError(f"intent {intent_id} not found")
    if status not in ALLOWED[row["status"]]:
        return False
    conn.execute(
        "UPDATE intents SET status = ?, http_status = COALESCE(?, http_status), record_ref = COALESCE(?, record_ref), "
        "note = COALESCE(?, note), reconciled = ?, updated_at = ? WHERE id = ?",
        (status, http_status, record_ref, note, 1 if reconciled else row["reconciled"], now_iso(), intent_id),
    )
    return True


def record_response(conn: sqlite3.Connection, intent_id: int, status: str, http_status: int | None, record_ref: str | None = None, note: str | None = None) -> bool:
    """Outcome from the HTTP response. Ignored if the intent was already reconciled:
    late responses never override what reconciliation established."""
    row = get(conn, intent_id)
    if row is not None and row["reconciled"]:
        return False
    return _transition(conn, intent_id, status, http_status, record_ref, note, reconciled=False)


def mark_reconciled(conn: sqlite3.Connection, intent_id: int, status: str, record_ref: str | None = None, note: str | None = None) -> bool:
    return _transition(conn, intent_id, status, None, record_ref, note, reconciled=True)


# --- reconciliation lookups -----------------------------------------------------------------------

_PARAM = re.compile(r"\{(\w+)\}")


def render(template: str, values: dict[str, Any]) -> str:
    """'{vendor_id}' -> 'V001'. Unknown names render as empty strings."""
    return _PARAM.sub(lambda m: str(values.get(m.group(1), "")), template)


def _norm(value: Any) -> str:
    return str(value if value is not None else "").strip().casefold()


@dataclass
class LookupResult:
    exists: bool | None              # None = the lookup itself failed (unknown)
    record_ref: str | None = None
    count: int = 0
    items: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


class ReconcileLookup:
    """Calls a connector's declared read API to decide whether a record exists."""

    def __init__(self, secrets: Any, attempts: int = 3, backoff: tuple[float, ...] = (0.5, 1.0, 2.0)) -> None:
        self.secrets = secrets
        self.attempts = attempts
        self.backoff = backoff

    def check(self, connector: ConnectorSpec, action: ActionSpec, values: dict[str, Any]) -> LookupResult:
        spec = action.reconcile
        if spec is None or connector.api is None:
            return LookupResult(exists=None, error="no reconcile lookup declared")
        endpoint = connector.api.endpoint(spec.api)
        if endpoint is None:
            return LookupResult(exists=None, error=f"endpoint {spec.api} not declared")
        params = {k: render(v, values) for k, v in spec.params.items()}
        path = endpoint.path
        for name in template_params(path):
            path = path.replace("{" + name + "}", params.pop(name, ""))
        url = (connector.base_url or "").rstrip("/") + path
        last_error = ""
        for attempt in range(self.attempts):
            try:
                response = httpx.get(url, params=params, headers=self.secrets.api_header(connector), timeout=10)
                if response.status_code >= 500:
                    last_error = f"HTTP {response.status_code}"
                elif response.status_code == 404:
                    return LookupResult(exists=False, count=0)
                else:
                    response.raise_for_status()
                    return self._decide(spec, response.json(), values)
            except (httpx.HTTPError, ValueError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            if attempt < self.attempts - 1:
                time.sleep(self.backoff[min(attempt, len(self.backoff) - 1)])
        return LookupResult(exists=None, error=f"lookup failed after {self.attempts} attempts ({last_error})")

    @staticmethod
    def _decide(spec: Any, data: Any, values: dict[str, Any]) -> LookupResult:
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict) and isinstance(data.get("items"), list):
            items = data["items"]
        elif isinstance(data, dict):
            items = [data]
        else:
            items = []
        if spec.exists_if == "non_empty_list":
            matches = items
        else:
            wanted = {f: _norm(render(t, values)) for f, t in (spec.match or {}).items()}
            matches = [it for it in items if isinstance(it, dict) and all(_norm(it.get(f)) == v for f, v in wanted.items())]
        ref = None
        if matches and spec.record_ref_field:
            ref = str(matches[0].get(spec.record_ref_field)) if matches[0].get(spec.record_ref_field) is not None else None
        return LookupResult(exists=bool(matches), record_ref=ref, count=len(matches), items=matches)


def payload_values(row: sqlite3.Row) -> dict[str, Any]:
    return loads(row["payload_json"], {})

"""The gateway: the single enforcement choke point (plan §11.7).

Every browser request (via Playwright's context.route), every API call and
every file write passes through here. Prompts advise; this code enforces
(DECISIONS.md D3). Pipeline for a network request:

  1 host allowlist (fail closed, D4)    2 blocked paths (/__*, connector globs)
  3 GET/HEAD -> allowed                 4 non-GET must match a declared action
  5 read_only mode blocks writes        6 payload: drop volatile fields, coerce types
  7 policy engine: deny / approval      8 duplicate + pre-write existence check (D7)
  9 approval grant (scope hash, D5)     10 intent log, then send (D6)
  11 classify outcome; reconcile unknowns   12 audit every decision

`decide()` is a pure function of the request, the database and an injectable
lookup, so the pipeline is unit-testable without a browser.
"""

from __future__ import annotations

import fnmatch
import html
import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from email.parser import BytesParser
from typing import Any, Literal
from urllib.parse import parse_qsl, quote, urlsplit

import httpx

from karya.company import Company
from karya.connectors.models import ActionSpec, ConnectorSpec, normalize_origin
from karya.db import audit
from karya.gateway import approvals, intents
from karya.gateway.intents import LookupResult, ReconcileLookup, render
from karya.gateway.policy import evaluate, referenced_fields
from karya.gateway.secrets import Secrets
from karya.runtime.models import PolicyEvent

Mode = Literal["act", "read_only"]
READ_METHODS = ("GET", "HEAD", "OPTIONS")


@dataclass
class RequestInfo:
    method: str
    url: str
    body: bytes | None = None
    content_type: str = ""
    is_navigation: bool = True


@dataclass
class Decision:
    kind: Literal["continue", "abort", "fulfill", "send"]
    status: int = 200
    body: str = ""
    connector: ConnectorSpec | None = None
    action: ActionSpec | None = None
    payload: dict[str, str] = field(default_factory=dict)   # raw strings, volatile fields dropped
    fields: dict[str, Any] = field(default_factory=dict)    # typed per the connector spec
    idem_key: str | None = None
    approval_id: str | None = None
    reason: str = ""


@dataclass
class ApiResult:
    ok: bool
    status: int | None
    data: Any = None
    error: str | None = None
    retries: int = 0
    url: str = ""


TONES = {"block": "#c0392b", "hold": "#b7791f", "info": "#2f6fb0"}


def page(title: str, message: str, detail: str = "", tone: str = "block") -> str:
    """The HTML the browser shows when the gateway answers instead of the app
    (it also appears in the task's screenshots, so it is styled to be readable)."""
    color = TONES.get(tone, TONES["block"])
    extra = f"<p>{html.escape(detail)}</p>" if detail else ""
    style = (
        "body{font-family:-apple-system,'Segoe UI',Roboto,Arial,sans-serif;background:#f5f7fa;margin:0;padding:40px}"
        f".card{{max-width:760px;margin:0 auto;background:#fff;border:1px solid #d9e2ec;border-left:6px solid {color};border-radius:6px;padding:18px 26px}}"
        f"h1{{font-size:22px;margin:0 0 10px;color:{color}}}p{{font-size:15px;line-height:1.5;color:#1f2933}}small{{color:#616e7c}}"
    )
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)} — Karya gateway</title><style>{style}</style></head>"
        f"<body><main class='card'><h1>{html.escape(title)}</h1><p role='alert'>{html.escape(message)}</p>{extra}"
        "<p><small>Answered by the Karya gateway, not by the company app.</small></p></main></body></html>"
    )


def parse_body(body: bytes | None, content_type: str) -> dict[str, str]:
    if not body:
        return {}
    ct = (content_type or "").lower()
    try:
        if "application/json" in ct:
            data = json.loads(body)
            return {str(k): "" if v is None else str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        if "multipart/form-data" in ct:
            message = BytesParser().parsebytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
            out: dict[str, str] = {}
            for part in message.walk():
                name = part.get_param("name", header="content-disposition")
                if name and not part.get_filename():
                    out[str(name)] = (part.get_payload(decode=True) or b"").decode("utf-8", "replace")
            return out
        return dict(parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True))
    except (ValueError, UnicodeDecodeError):
        return {}


def coerce(value: str, type_: str) -> Any:
    text = value.strip()
    if type_ == "decimal":
        try:
            return Decimal(text)
        except InvalidOperation as exc:
            raise ValueError(f"not a decimal: {value!r}") from exc
    if type_ == "date":
        return date.fromisoformat(text)
    if type_ == "int":
        return int(text)
    return text


def canonical(value: Any) -> str:
    if isinstance(value, Decimal):
        return f"{value:.2f}"
    if isinstance(value, date):
        return value.isoformat()
    return str(value).strip().casefold()


class Gateway:
    def __init__(
        self,
        task_id: str,
        company: Company,
        conn: sqlite3.Connection,
        secrets: Secrets,
        mode: Mode = "act",
        lookup: ReconcileLookup | None = None,
        actor: str = "agent",
    ) -> None:
        self.task_id = task_id
        self.company = company
        self.conn = conn
        self.secrets = secrets
        self.mode = mode
        self.lookup = lookup or ReconcileLookup(secrets)
        self.actor = actor
        self.events: list[PolicyEvent] = []
        self.label = company.profile.short_name or company.name
        self._in_flight: set[int] = set()  # intents whose request is being sent right now

    # --- helpers -------------------------------------------------------------------------------

    def drain_events(self) -> list[PolicyEvent]:
        """Tools collect the events produced while one action ran."""
        events, self.events = self.events, []
        return events

    def _event(self, event: PolicyEvent, audit_event: str, details: dict[str, Any]) -> None:
        self.events.append(event)
        self._audit(audit_event, {**details, "message": event.message})

    def _audit(self, event: str, details: dict[str, Any]) -> None:
        audit(self.conn, "gateway", event, {"mode": self.mode, "actor": self.actor, **details}, self.task_id)

    # --- 1-9: the decision ---------------------------------------------------------------------

    def decide(self, req: RequestInfo) -> Decision:
        parts = urlsplit(req.url)
        if parts.scheme in ("data", "blob", "about"):
            return Decision("continue")
        method = req.method.upper()
        connector = self.company.connector_for_origin(normalize_origin(req.url)) if parts.scheme in ("http", "https") else None
        if connector is None:  # 1. host allowlist, fail closed
            message = f"Blocked: {parts.scheme}://{parts.netloc} is not an approved company system."
            if req.is_navigation or method not in READ_METHODS:
                self._event(PolicyEvent(type="blocked_host", rule_ids=["unknown_host"], message=message), "blocked_host", {"url": req.url, "method": method})
            else:
                self._audit("blocked_host", {"url": req.url, "method": method, "subresource": True})
            return Decision("abort", reason=message)

        path = parts.path or "/"
        if path.startswith("/__") or connector.is_blocked_path(path):  # 2. blocked paths
            message = "Blocked: this path is not available to the AI employee."
            self._event(PolicyEvent(type="denied", rule_ids=["blocked_path"], message=message), "blocked_path", {"url": req.url, "method": method})
            return Decision("fulfill", status=403, body=page("Blocked", message, f"{method} {path}"), connector=connector)

        if method in READ_METHODS:  # 3. reads are allowed
            return Decision("continue", connector=connector)

        matched = connector.match_action(method, path)  # 4. declared actions only
        if matched is None:
            message = f"Blocked by default: undeclared write ({method} {path} is not a declared action of {connector.name})."
            self._event(PolicyEvent(type="denied", rule_ids=["unknown_write"], message=message), "unknown_write", {"url": req.url, "method": method})
            return Decision("fulfill", status=403, body=page("Blocked", message), connector=connector)
        action, path_params = matched

        if action.risk == "auth":  # login/logout: allowed in every mode, payload never inspected
            return Decision("continue", connector=connector, action=action)

        if self.mode == "read_only":  # 5. the verifier cannot write
            message = f"Verifier is read-only: {method} {path} ({connector.id}.{action.name}) was blocked."
            self._event(PolicyEvent(type="read_only_block", message=message), "read_only_block", {"connector": connector.id, "action": action.name})
            return Decision("fulfill", status=403, body=page("Blocked", message), connector=connector, action=action)

        # 6. payload: drop volatile fields, add path parameters, coerce declared types
        payload = {k: v for k, v in parse_body(req.body, req.content_type).items() if k not in connector.volatile_fields}
        payload.update(path_params)
        fields: dict[str, Any] = {}
        for name, raw in payload.items():
            spec = action.fields.get(name)
            try:
                fields[name] = coerce(raw, spec.type) if spec else raw
            except ValueError:
                fields[name] = raw  # left as a string: the policy engine then fails closed
        base = {"connector": connector.id, "action": action.name, "payload": payload}

        # 7. policy
        policy = evaluate(self.company.policies, connector.id, action.name, fields)
        if policy.effect == "deny":
            message = f"Blocked by {self.label} policy {', '.join(policy.deciding_ids)}: {' '.join(r.description for r in policy.deciding)} This attempt has been logged."
            self._event(PolicyEvent(type="denied", rule_ids=policy.deciding_ids, message=message), "policy_denied", {**base, "rule_ids": policy.rule_ids})
            return Decision("fulfill", status=403, body=page("Blocked by policy", message), connector=connector, action=action, payload=payload, fields=fields)

        # 8. duplicates: earlier attempts in this task, then the system of record itself
        idem_key = None
        if action.idempotency_key and all(fields.get(k) not in (None, "") for k in action.idempotency_key):
            idem_key = f"{connector.id}:{action.name}:" + "|".join(canonical(fields[k]) for k in action.idempotency_key)
        if idem_key and action.reconcile:
            duplicate = self._duplicate_check(connector, action, payload, idem_key)
            if duplicate is not None:
                return duplicate

        # 9. approvals bound to the exact payload
        approval_id = None
        if policy.effect == "require_approval":
            scope = approvals.scope_hash(connector.id, action.name, fields, set(action.idempotency_key) | referenced_fields(policy.deciding))
            approved = approvals.find_by_scope(self.conn, self.task_id, scope, "approved")
            rejected = approvals.find_by_scope(self.conn, self.task_id, scope, "rejected")
            rule_text = "; ".join(f"rule {r.id}: {r.description.rstrip('.')}" for r in policy.deciding)
            if approved is not None:
                approval_id = approved["id"]
                message = f"Using approval {approval_id} granted by {approved['decided_by']} ({rule_text})."
                self._event(PolicyEvent(type="approved_use", rule_ids=policy.deciding_ids, approval_id=approval_id, message=message), "approved_use", base)
            elif rejected is not None:
                message = f"Rejected by {rejected['decided_by']} ({rejected['id']}): {rejected['reason']}. Do not resubmit; report the rejection."
                self._event(PolicyEvent(type="denied", rule_ids=policy.deciding_ids, approval_id=rejected["id"], message=message), "approval_rejected_block", base)
                return Decision("fulfill", status=403, body=page("Rejected by approver", message), connector=connector, action=action, payload=payload, fields=fields)
            else:
                summary = render(action.display, payload) if action.display else f"{connector.id}.{action.name} {payload}"
                approval, created = approvals.create_or_reuse(
                    self.conn, self.task_id, connector.id, action.name, policy.deciding_ids,
                    ",".join(policy.approver_roles), payload, scope, summary,
                )
                message = f"Held for approval {approval['id']} ({rule_text}). Do not resubmit until approved."
                self._event(
                    PolicyEvent(type="approval_required", rule_ids=policy.deciding_ids, approval_id=approval["id"], message=message),
                    "approval_required",
                    {**base, "approval_id": approval["id"], "created": created},
                )
                detail = f"Approver role: {approval['approver_role']}. {summary}"
                return Decision("fulfill", status=202, body=page("Held for approval", message, detail, tone="hold"), connector=connector, action=action, payload=payload, fields=fields, approval_id=approval["id"])

        return Decision("send", connector=connector, action=action, payload=payload, fields=fields, idem_key=idem_key, approval_id=approval_id)

    def _duplicate_check(self, connector: ConnectorSpec, action: ActionSpec, payload: dict[str, str], idem_key: str) -> Decision | None:
        keys = " and ".join(action.idempotency_key)
        for row in intents.find_by_idem(self.conn, self.task_id, idem_key):
            status = row["status"]
            if row["id"] in self._in_flight:  # e.g. a double-click: never reconcile a request still in flight
                message = "An identical submission is being sent right now. Not submitting it twice."
                self._event(PolicyEvent(type="duplicate_prevented", intent_id=row["id"], message=message), "duplicate_prevented", {"idem_key": idem_key, "intent_id": row["id"], "in_flight": True})
                return Decision("fulfill", status=409, body=page("Duplicate prevented", message, tone="info"), connector=connector, action=action, payload=payload)
            if status in ("pending", "unknown"):
                status = self._reconcile_row(row, http_status=row["http_status"])
            if status == "committed":
                ref = intents.get(self.conn, row["id"])["record_ref"] or "see earlier step"
                message = f"Already done in this task ({ref}). Not submitting again."
                self._event(PolicyEvent(type="duplicate_prevented", intent_id=row["id"], message=message), "duplicate_prevented", {"idem_key": idem_key, "intent_id": row["id"]})
                return Decision("fulfill", status=409, body=page("Duplicate prevented", message, tone="info"), connector=connector, action=action, payload=payload)
            if status in ("pending", "unknown"):
                message = "The outcome of an earlier attempt is still unknown and could not be checked. Not submitting again; try later."
                self._event(PolicyEvent(type="duplicate_prevented", intent_id=row["id"], status="unknown", message=message), "duplicate_prevented", {"idem_key": idem_key, "intent_id": row["id"]})
                return Decision("fulfill", status=409, body=page("Duplicate prevented", message, tone="info"), connector=connector, action=action, payload=payload)
        # Deterministic backstop (D7): the record may exist already, e.g. posted earlier by a person.
        found = self.lookup.check(connector, action, payload)
        if found.exists:
            ref = f" ({found.record_ref})" if found.record_ref else ""
            message = f"A record with the same {keys} already exists{ref}. Not posting a duplicate."
            self._event(PolicyEvent(type="duplicate_prevented", message=message), "duplicate_prevented", {"idem_key": idem_key, "existing": found.record_ref})
            return Decision("fulfill", status=409, body=page("Duplicate prevented", message, tone="info"), connector=connector, action=action, payload=payload)
        if found.exists is None:
            message = f"Could not check {connector.name} for an existing record ({found.error}). Not submitting; try again shortly."
            self._event(PolicyEvent(type="denied", rule_ids=["precheck_unavailable"], message=message), "precheck_unavailable", {"idem_key": idem_key})
            return Decision("fulfill", status=503, body=page("Not submitted", message, tone="info"), connector=connector, action=action, payload=payload)
        return None

    # --- 10-11: send, observe, reconcile ---------------------------------------------------------

    def on_route(self, route: Any, request: Any = None) -> None:
        """Playwright route handler for EVERY request of the task's browser context."""
        request = request or route.request
        info = RequestInfo(
            method=request.method,
            url=request.url,
            body=request.post_data_buffer,
            content_type=request.headers.get("content-type", ""),
            is_navigation=request.is_navigation_request(),
        )
        try:
            decision = self.decide(info)
        except Exception as exc:  # fail closed: an error in the gateway never lets a request through
            self._audit("gateway_error", {"url": info.url, "error": f"{type(exc).__name__}: {exc}"})
            route.fulfill(status=500, content_type="text/html; charset=utf-8", body=page("Gateway error", "The request was not sent.", str(exc)))
            return
        if decision.kind == "continue":
            route.continue_()
        elif decision.kind == "abort":
            route.abort("blockedbyclient")
        elif decision.kind == "fulfill":
            route.fulfill(status=decision.status, content_type="text/html; charset=utf-8", body=decision.body)
        else:
            self._send(route, decision)

    def _send(self, route: Any, decision: Decision) -> None:
        connector, action = decision.connector, decision.action
        assert connector is not None and action is not None
        intent_id = intents.create(self.conn, self.task_id, connector.id, action.name, decision.idem_key, decision.payload, decision.approval_id)
        self._audit("intent_created", {"intent_id": intent_id, "connector": connector.id, "action": action.name, "payload": decision.payload})
        self._in_flight.add(intent_id)
        try:
            try:
                response = route.fetch(max_redirects=0, timeout=15_000)
            except Exception as exc:  # timeout / connection reset: the outcome is unknown
                message = self.resolve_unknown(intent_id, connector, action, decision.payload, None, f"{type(exc).__name__}: {exc}", decision.approval_id)
                route.fulfill(status=504, content_type="text/html; charset=utf-8", body=page("No response", message, tone="info"))
                return
            self.record_outcome(intent_id, connector, action, decision.payload, response.status, response.headers.get("location", ""), decision.approval_id)
        finally:
            self._in_flight.discard(intent_id)
        route.fulfill(response=response)  # the browser follows the 303 as usual

    def record_outcome(
        self,
        intent_id: int,
        connector: ConnectorSpec,
        action: ActionSpec,
        payload: dict[str, str],
        status: int,
        location: str = "",
        approval_id: str | None = None,
    ) -> str:
        """Classify the HTTP outcome of a write. Returns the final intent status."""
        signal = connector.auth.logged_out_signal if connector.auth else None
        if signal and 300 <= status < 400 and urlsplit(location).path.startswith(signal.path_prefix):
            note = "session expired: redirected to the login page; nothing was submitted"
            intents.record_response(self.conn, intent_id, "failed", status, note=note)
            message = f"{connector.name} redirected to its login page: the session had expired and the form was NOT submitted. Call browser_login('{connector.id}'), then refill the form and resubmit."
            self._event(PolicyEvent(type="write_outcome", intent_id=intent_id, status="failed", message=message), "write_outcome", {"intent_id": intent_id, "status": "failed", "http_status": status})
            return "failed"
        if 200 <= status < 400:
            ref = None
            if action.reconcile:
                # The system of record is the source of truth, not the status code.
                found = self.lookup.check(connector, action, payload)
                if found.exists is False:
                    intents.record_response(self.conn, intent_id, "failed", status, note=f"HTTP {status} but no record exists")
                    message = f"{connector.name} answered HTTP {status}, but no matching record exists. The write did not take effect; read the page and try again."
                    self._event(PolicyEvent(type="write_outcome", intent_id=intent_id, status="failed", message=message), "write_outcome", {"intent_id": intent_id, "status": "failed", "http_status": status})
                    return "failed"
                ref = found.record_ref
            intents.record_response(self.conn, intent_id, "committed", status, record_ref=ref)
            if approval_id:
                approvals.consume(self.conn, approval_id)
            what = render(action.display, payload) if action.display else f"{connector.id}.{action.name}"
            message = f"Write committed: {what}" + (f" -> {ref}." if ref else ".")
            self._event(PolicyEvent(type="write_outcome", intent_id=intent_id, status="committed", approval_id=approval_id, message=message), "write_outcome", {"intent_id": intent_id, "status": "committed", "http_status": status, "record_ref": ref})
            return "committed"
        if 400 <= status < 500:
            intents.record_response(self.conn, intent_id, "failed", status, note=f"HTTP {status}")
            message = f"{connector.name} rejected the write (HTTP {status}). Nothing was saved. Read the page for the reason, fix it and resubmit."
            self._event(PolicyEvent(type="write_outcome", intent_id=intent_id, status="failed", message=message), "write_outcome", {"intent_id": intent_id, "status": "failed", "http_status": status})
            return "failed"
        self.resolve_unknown(intent_id, connector, action, payload, status, f"HTTP {status}", approval_id)
        return intents.get(self.conn, intent_id)["status"]

    def resolve_unknown(
        self,
        intent_id: int,
        connector: ConnectorSpec,
        action: ActionSpec,
        payload: dict[str, str],
        http_status: int | None,
        reason: str,
        approval_id: str | None = None,
    ) -> str:
        """5xx / timeout: never guess and never retry blindly. Check reality (D6)."""
        intents.record_response(self.conn, intent_id, "unknown", http_status, note=reason)
        what = f"{connector.name} returned {reason}"
        if not action.reconcile:
            message = f"{what}; this action cannot be checked automatically, so its outcome is unknown. Do not resubmit; check the record yourself."
            self._event(PolicyEvent(type="write_outcome", intent_id=intent_id, status="unknown", message=message), "write_outcome", {"intent_id": intent_id, "status": "unknown"})
            return message
        found = self.lookup.check(connector, action, payload)
        message = self._apply_reconcile(intent_id, found, what, approval_id)
        return message

    def _apply_reconcile(self, intent_id: int, found: LookupResult, what: str, approval_id: str | None) -> str:
        if found.exists:
            intents.mark_reconciled(self.conn, intent_id, "committed", record_ref=found.record_ref, note="reconciled: record exists")
            if approval_id:
                approvals.consume(self.conn, approval_id)
            ref = f" ({found.record_ref})" if found.record_ref else ""
            message = f"{what}, but the record exists{ref}. The write succeeded; do not resubmit."
            status = "committed"
        elif found.exists is False:
            intents.mark_reconciled(self.conn, intent_id, "failed", note="reconciled: no record")
            message = f"{what} and no record exists. The write did not happen; it is safe to submit again."
            status = "failed"
        else:
            message = f"{what} and the record lookup failed ({found.error}), so the outcome is unknown. Do not resubmit now; the gateway re-checks before any retry."
            status = "unknown"
        self._event(PolicyEvent(type="write_outcome", intent_id=intent_id, status=status, approval_id=approval_id, message=message), "reconcile", {"intent_id": intent_id, "status": status, "record_ref": found.record_ref})
        return message

    def _reconcile_row(self, row: sqlite3.Row, http_status: int | None = None) -> str:
        connector = self.company.connectors.get(row["connector"])
        action = connector.action(row["action"]) if connector else None
        if connector is None or action is None or action.reconcile is None:
            return row["status"]
        found = self.lookup.check(connector, action, intents.payload_values(row))
        what = f"An earlier {connector.id}.{action.name} attempt (intent {row['id']}) had an unknown outcome"
        self._apply_reconcile(row["id"], found, what, row["approval_id"])
        return intents.get(self.conn, row["id"])["status"]

    def reconcile_unresolved(self) -> list[PolicyEvent]:
        """On resume: settle writes that were in flight when the worker stopped."""
        for row in intents.unresolved(self.conn, self.task_id):
            if row["status"] == "pending":
                intents.record_response(self.conn, row["id"], "unknown", None, note="worker stopped mid-write")
            self._reconcile_row(intents.get(self.conn, row["id"]))
        return self.drain_events()

    # --- API reads -------------------------------------------------------------------------------

    def api_get(self, connector_id: str, endpoint_name: str, params: dict[str, Any]) -> ApiResult:
        connector = self.company.connectors.get(connector_id)
        if connector is None or connector.api is None:
            available = [c.id for c in self.company.connectors.values() if c.api]
            return ApiResult(False, None, error=f"No API connector {connector_id!r}. Connectors with APIs: {available}")
        endpoint = connector.api.endpoint(endpoint_name)
        if endpoint is None:
            names = [e.name for e in connector.api.endpoints]
            return ApiResult(False, None, error=f"{connector_id} has no declared endpoint {endpoint_name!r}. Declared: {names}")
        query = {str(k): str(v) for k, v in (params or {}).items() if v not in (None, "")}
        path = endpoint.path
        for name in endpoint.path_params:
            if name not in query:
                return ApiResult(False, None, error=f"{endpoint_name} needs the path parameter {name!r}")
            path = path.replace("{" + name + "}", quote(query.pop(name), safe=""))
        unknown = sorted(set(query) - set(endpoint.params))
        if unknown:
            return ApiResult(False, None, error=f"{endpoint_name} does not accept {unknown}; allowed: {endpoint.params}")
        if path.startswith("/__") or connector.is_blocked_path(path):
            return ApiResult(False, None, error="Blocked: this path is not available to the AI employee.")
        url = (connector.base_url or "").rstrip("/") + path
        retries, last_error, response = 0, "", None
        for attempt in range(3):
            try:
                response = httpx.get(url, params=query, headers=self.secrets.api_header(connector), timeout=10)
            except httpx.HTTPError as exc:
                last_error, response = f"{type(exc).__name__}: {exc}", None
            if response is not None and response.status_code < 500:
                break
            if attempt < 2:
                retries += 1
                time.sleep((1.0, 3.0)[attempt])
        if response is None:
            return ApiResult(False, None, error=last_error, retries=retries, url=url)
        try:
            data: Any = response.json()
        except ValueError:
            data = response.text[:2000]
        error = None if response.status_code < 400 else f"HTTP {response.status_code}"
        return ApiResult(response.status_code < 400, response.status_code, data=data, error=error, retries=retries, url=url)

    # --- file writes -------------------------------------------------------------------------------

    def check_file_write(self, root: str, rel_path: str) -> tuple[bool, str]:
        """Writes go only to the task workspace or a filesystem connector's write globs."""
        if self.mode == "read_only":
            ok, why = False, "Verifier is read-only."
        elif root == "workspace":
            ok, why = True, "task workspace"
        else:
            connector = self.company.connectors.get(root)
            globs = connector.write if connector and connector.kind == "filesystem" else []
            ok = any(fnmatch.fnmatchcase(rel_path, pattern) for pattern in globs)
            why = f"matches {root} write globs {globs}" if ok else f"{root}/{rel_path} is not writable (allowed: {globs or 'none'})"
        self._audit("file_write" if ok else "file_write_denied", {"root": root, "path": rel_path, "why": why})
        if not ok:
            self.events.append(PolicyEvent(type="denied", rule_ids=["file_write"], message=f"Blocked file write: {why}"))
        return ok, why

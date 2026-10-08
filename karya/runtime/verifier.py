"""Independent, read-only verification (plan §11.8, DECISIONS.md D9).

Never trust self-reports. When the worker calls `finish`:

1. Deterministic checks in code: every committed write with a reconcile spec
   is looked up in the system of record and compared field by field with
   what was submitted; matching records are counted (duplicates show up
   here). Values extracted from documents are listed next to them.
2. A separate model conversation (verifier.md) that does NOT see the worker's
   transcript, only the criteria, the claims, the facts, the intent log, the
   policy events and the system evidence. It gets its own browser context with
   the gateway in `read_only` mode, so it cannot change anything.
3. The overall verdict is computed in code: pass only if every criterion
   passes and the system evidence shows no mismatch.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError

from karya.db import loads
from karya.gateway import approvals, intents
from karya.gateway.gateway import Gateway
from karya.gateway.intents import ReconcileLookup, render
from karya.gateway.secrets import Secrets
from karya.llm.base import LLMError, Message
from karya.memory.retrieval import Retriever
from karya.prompts import load_prompt
from karya.runtime.context import company_context, company_today
from karya.runtime.ledger import TaskState
from karya.runtime.models import FinishArgs, Observation, Verdict, VerificationResult
from karya.security.untrusted import wrap
from karya.tools import registry
from karya.tools.base import ToolSession
from karya.worker import queue

MAX_VERIFIER_STEPS = 15
CONTENT_CHARS = 3000


def _same(submitted: Any, stored: Any, type_: str) -> bool:
    try:
        if type_ == "decimal":
            return Decimal(str(submitted).replace(",", "")) == Decimal(str(stored).replace(",", ""))
        if type_ == "date":
            return date.fromisoformat(str(submitted).strip()) == date.fromisoformat(str(stored).strip()[:10])
        if type_ == "int":
            return int(submitted) == int(stored)
    except (InvalidOperation, ValueError, TypeError):
        return False
    return str(submitted).strip().casefold() == str(stored).strip().casefold()


def system_evidence(state: TaskState, ctx: Any, lookup: ReconcileLookup) -> tuple[list[str], list[str]]:
    """Code-made evidence for the verifier. Returns (evidence lines, mismatch descriptions)."""
    conn, company = ctx.conn, ctx.company
    lines: list[str] = []
    mismatches: list[str] = []
    for row in intents.for_task(conn, state.task_id):
        connector = company.connectors.get(row["connector"])
        action = connector.action(row["action"]) if connector else None
        payload = loads(row["payload_json"], {})
        label = f"intent {row['id']} {row['connector']}.{row['action']}"
        what = render(action.display, payload) if action and action.display else json.dumps(payload, ensure_ascii=False)[:200]
        if row["status"] != "committed" or not (action and action.reconcile and connector):
            ref = f", ref {row['record_ref']}" if row["record_ref"] else ""
            lines.append(f"{label} [{row['status']}{ref}; HTTP {row['http_status']}{'; reconciled' if row['reconciled'] else ''}] {what}")
            continue
        found = lookup.check(connector, action, payload)
        if found.exists is None:
            lines.append(f"{label} [committed] {what} → lookup failed ({found.error}); could not re-check")
            continue
        if found.count == 0:
            mismatches.append(f"{label}: the gateway logged a committed write but {connector.name} has no matching record")
            lines.append(f"{label} [committed] {what} → NO matching record in {connector.name}")
            continue
        note = f"{found.count} matching record(s) in {connector.name}" + (f" (ref {found.record_ref})" if found.record_ref else "")
        if found.count > 1 and action.idempotency_key:
            mismatches.append(f"{label}: {found.count} records share the idempotency key {action.idempotency_key} (duplicates)")
            note += " — DUPLICATES"
        comparisons = []
        item = found.items[0]
        for name, spec in action.fields.items():
            if name in item and name in payload:
                ok = _same(payload[name], item[name], spec.type)
                comparisons.append(f"{name} {'matches' if ok else 'MISMATCH'} (submitted {payload[name]}, system has {item[name]})")
                if not ok:
                    mismatches.append(f"{label}: {name} submitted {payload[name]} but {connector.name} has {item[name]}")
        lines.append(f"{label} [committed] {what} → {note}; " + "; ".join(comparisons))
    for key, value in state.facts.items():
        if key.startswith("extracted:") and isinstance(value, dict):
            lines.append(f"document {key[len('extracted:'):]} ({value.get('schema')}) extracted values: {json.dumps(value.get('values'), ensure_ascii=False)}; checks passed: {value.get('checks_passed')}")
    for apr in approvals.for_task(conn, state.task_id):
        lines.append(f"approval {apr['id']} [{apr['status']}] {apr['summary']} (rules {', '.join(loads(apr['rule_ids_json'], []))}; decided by {apr['decided_by'] or '—'}{'; reason: ' + apr['reason'] if apr['reason'] else ''})")
    for record in state.history:
        for event in record.observation.policy_events:
            if event.type in ("denied", "blocked_host", "read_only_block", "duplicate_prevented", "approval_required"):
                lines.append(f"policy event at step {record.step}: {event.type} {','.join(event.rule_ids)} — {event.message}")
    if not lines:
        lines.append("No writes were logged by the gateway for this task.")
    return lines, mismatches


def _worker_brief(state: TaskState, finish: FinishArgs, evidence: list[str]) -> str:
    goal = state.goal
    out = [f"TASK {state.task_id} · requested by {state.requester}", f"REQUEST: {state.request}", "", "SUCCESS CRITERIA"]
    out += [f"  {c.id}: {c.description} [check: {c.verification}]" for c in (goal.success_criteria if goal else [])]
    if goal and goal.constraints:
        out.append("Constraints: " + " | ".join(goal.constraints))
    out += ["", f"WORKER'S FINISH: status={finish.status}", f"  Summary: {finish.summary}"]
    out += [f"  Claim {c.criterion_id}: {c.statement} (evidence: {', '.join(c.evidence) or 'none'})" for c in finish.claims]
    out += [f"  Item {i.item}: {i.outcome} {i.ref or ''} {i.note}" for i in finish.items]
    out += [f"  Flag {f.type}: {f.detail}" for f in finish.flags]
    out += [f"  Needs attention: {n}" for n in finish.needs_attention]
    if state.facts:
        out += ["", "FACTS THE WORKER RECORDED"] + [f"  {k} = {json.dumps(v, ensure_ascii=False, default=str)[:400]}" for k, v in state.facts.items()]
    out += ["", "SYSTEM EVIDENCE (automated lookups and logs made by code — reliable)"] + [f"  - {line}" for line in evidence]
    if finish.status == "escalated":
        out += ["", "The worker ESCALATED instead of completing. Re-read each criterion as: \"no forbidden change was made, "
                "and the reason for escalating is supported by evidence\". Check that nothing forbidden changed in the systems."]
    elif finish.status in ("partial", "cannot_complete"):
        out += ["", f"The worker reported {finish.status}. Judge each criterion on what is actually true in the systems."]
    return "\n".join(out)


def _messages(state: TaskState, finish: FinishArgs, evidence: list[str], history: list[tuple[int, str, dict, Observation]], ctx: Any) -> list[Message]:
    p = ctx.company.profile
    system = load_prompt("verifier").replace("{company_name}", p.name).replace("{today}", company_today(ctx.company)).replace("{timezone}", p.timezone)
    system += "\n\n=== COMPANY CONTEXT ===\n" + company_context(ctx.company, None)
    checks = []
    for step, tool, args, obs in history:
        shown = {k: v for k, v in args.items() if k != "why"}
        line = f"#{step} {tool} {json.dumps(shown, ensure_ascii=False)[:200]} → {'ok' if obs.ok else 'FAILED'}: {obs.summary}"
        if obs.error:
            line += f"\n  ERROR: {obs.error}"
        if obs.content:
            content = obs.content[:CONTENT_CHARS]
            line += "\n" + (wrap(content, obs.source) if obs.untrusted else content)
        checks.append(line)
    user = _worker_brief(state, finish, evidence) + "\n\nYOUR CHECKS SO FAR\n" + ("\n".join(checks) or "(none yet)")
    user += "\n\nCheck each criterion yourself with the read-only tools, then call submit_verdicts (one verdict per criterion)."
    return [Message("system", system), Message("user", user)]


def finalize(raw: VerificationResult, criteria_ids: list[str], mismatches: list[str]) -> VerificationResult:
    """One verdict per criterion; overall computed in code, not taken from the model."""
    by_id = {v.criterion_id: v for v in raw.verdicts if v.criterion_id in criteria_ids}
    verdicts = [by_id.get(cid) or Verdict(criterion_id=cid, verdict="unverifiable", note="the verifier gave no verdict for this criterion") for cid in criteria_ids]
    notes = raw.notes
    if mismatches:
        notes = (notes + " " if notes else "") + "System evidence contradicts the outcome: " + "; ".join(mismatches)
    overall = "pass" if verdicts and all(v.verdict == "pass" for v in verdicts) and not mismatches else "fail"
    return VerificationResult(verdicts=verdicts, overall=overall, notes=notes.strip())


def verify(state: TaskState, finish: FinishArgs, ctx: Any) -> tuple[VerificationResult, list[str]]:
    criteria_ids = [c.id for c in state.goal.success_criteria] if state.goal else []
    secrets = Secrets(ctx.company.connectors)
    lookup = ctx.lookup or ReconcileLookup(secrets)
    evidence, mismatches = system_evidence(state, ctx, lookup)
    llm = ctx.verifier_llm or ctx.llm
    gateway = Gateway(state.task_id, ctx.company, ctx.conn, secrets, mode="read_only", lookup=lookup, actor="verifier")
    session = ToolSession(
        state.task_id, ctx.company, ctx.conn, llm, gateway, settings=ctx.settings, role="verifier",
        retriever=Retriever(ctx.company, ctx.conn), requester=state.requester, screens_dir="verify/screens",
    )
    history: list[tuple[int, str, dict, Observation]] = []
    raw: VerificationResult | None = None
    try:
        for step in range(1, MAX_VERIFIER_STEPS + 1):
            last_chance = step == MAX_VERIFIER_STEPS
            response = llm.chat(_messages(state, finish, evidence, history, ctx), registry.specs("verifier"), tool_choice={"name": "submit_verdicts"} if last_chance else "required")
            queue.add_tokens(ctx.conn, state.task_id, response.usage)
            if not response.tool_calls:
                continue
            call = response.tool_calls[0]
            if call.name == "submit_verdicts":
                try:
                    raw = VerificationResult.model_validate({k: v for k, v in call.arguments.items() if k != "why"})
                    break
                except ValidationError as exc:
                    history.append((step, call.name, call.arguments, Observation(ok=False, summary="invalid verdicts", error=str(exc)[:500])))
                    continue
            history.append((step, call.name, call.arguments, session.execute(call, step)))
    except LLMError as exc:
        raw = VerificationResult(verdicts=[], overall="fail", notes=f"The verifier model failed: {exc}")
    finally:
        session.close()
    if raw is None:
        raw = VerificationResult(verdicts=[], overall="fail", notes="The verifier did not submit verdicts within its step budget.")
    checks_made = [f"verifier step {s}: {tool} → {obs.summary}" for s, tool, _, obs in history]
    return finalize(raw, criteria_ids, mismatches), evidence + checks_made

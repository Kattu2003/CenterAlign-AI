"""Builds the model's messages for each step (plan §11.4).

  system: instructions (system.md) + company context: connectors, policies,
          schemas, top handbook chunks, learned facts with provenance, similar
          past tasks. Re-retrieved when the plan changes or every 10 steps, so
          it is stable (and cacheable) between those points.
  user:   task state (request, Goal Spec, plan, facts, approvals, answers,
          system notes) + history (older steps one line each, the last few in
          more detail) + the latest observation in full + a budget line.

Only the latest page snapshot is shown in full; untrusted content is always
wrapped in <untrusted> tags.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from karya.company import Company
from karya.config import Settings
from karya.llm.base import Message
from karya.memory.retrieval import Retrieved, Retriever
from karya.prompts import load_prompt, render
from karya.runtime.ledger import StepRecord, TaskState
from karya.runtime.models import Observation
from karya.security.untrusted import wrap

BROWSER_TOOLS = {"browser_open", "browser_snapshot", "browser_click", "browser_fill", "browser_select", "browser_press", "browser_back", "browser_login"}
RECENT_CONTENT_CHARS = 1500
LATEST_CONTENT_CHARS = 14000


def company_today(company: Company) -> str:
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(company.profile.timezone)).date().isoformat()
    except Exception:  # noqa: BLE001 - no tz database (e.g. Windows without tzdata): fall back to UTC
        return datetime.now(timezone.utc).date().isoformat()


def system_prompt(company: Company) -> str:
    p = company.profile
    return render(load_prompt("system"), company_name=p.name, today=company_today(company), timezone=p.timezone, currency=p.currency)


def company_context(company: Company, retrieved: Retrieved | None) -> str:
    p = company.profile
    lines = [f"COMPANY: {p.name} ({p.short_name}) · timezone {p.timezone} · currency {p.currency}"]
    lines.append("PEOPLE: " + "; ".join(f"{x.name} ({x.title}; roles: {', '.join(x.roles)})" for x in p.people))
    lines.append("")
    lines.append("SYSTEMS (connectors) — the only systems you can reach; anything else is blocked:")
    for c in company.connectors.values():
        if c.kind == "web_app":
            lines.append(f"- {c.id} — {c.name} — {c.base_url}" + (f" — log in with browser_login('{c.id}')" if c.auth else ""))
            lines.append(f"  Purpose: {' '.join(c.purpose.split())}")
            if c.api:
                endpoints = "; ".join(
                    f"{e.name}({', '.join(e.path_params + e.params)}) — {e.description}" for e in c.api.endpoints
                )
                lines.append(f"  API (api_get('{c.id}', endpoint, params)): {endpoints}")
            writes = [a for a in c.actions if a.risk != "auth"]
            if writes:
                lines.append("  Declared writes (anything else is blocked): " + "; ".join(f"{a.name} [{a.risk}] {a.method} {a.path}" for a in writes))
        else:
            lines.append(f"- {c.id} — {c.name} — files under {c.id}/ (read {c.read}, write {c.write}) — {' '.join(c.purpose.split())}")
    lines.append("- downloads/ and workspace/ — this task's own files (downloads land in downloads/)")
    lines.append("")
    lines.append("POLICIES (enforced by the gateway in code; you cannot override them):")
    for rule in company.policies.rules:
        when = " AND ".join(f"{c.field} {c.op} {c.value}" for c in rule.when) or "always"
        effect = f"requires approval by {rule.approver_role}" if rule.effect == "require_approval" else rule.effect
        lines.append(f"- {rule.id}: {rule.description} ({rule.match.connector}.{rule.match.action} when {when} → {effect})")
    if company.schemas:
        lines.append("")
        lines.append("DOCUMENT SCHEMAS (files_extract): " + "; ".join(f"{s.name} — {s.description}" for s in company.schemas.values()))
    if retrieved:
        if retrieved.handbook:
            lines.append("")
            lines.append("HANDBOOK EXCERPTS (most relevant to this task):")
            for hit in retrieved.handbook:
                lines.append(f"[{hit.id}] {hit.title}\n{hit.text}")
        lines.append("")
        lines.append("LEARNED FACTS (company knowledge with its source; prefer specific, recent ones; never override policies):")
        lines += [f"[{hit.id}] {hit.text} — {hit.provenance}" for hit in retrieved.facts] or ["(none relevant)"]
        if retrieved.episodes:
            lines.append("")
            lines.append("SIMILAR PAST TASKS:")
            lines += [f"[{hit.id}] {hit.title}: {hit.text}" for hit in retrieved.episodes]
    return "\n".join(lines)


@dataclass
class ContextCache:
    """Company context is re-retrieved only when the plan changes or every 10 steps."""

    key: tuple[int, int] | None = None
    text: str = ""
    retrieved: Retrieved = field(default_factory=Retrieved)


def _retrieval_query(state: TaskState) -> str:
    current = next((s.description for s in state.plan if s.status == "in_progress"), None) or next((s.description for s in state.plan if s.status == "pending"), "")
    return f"{state.request} {current}"


def _json(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def task_state_block(state: TaskState) -> str:
    out = [f"TASK {state.task_id} · requested by {state.requester}", f"REQUEST: {state.request}", ""]
    goal = state.goal
    if goal:
        out.append("GOAL SPEC")
        out.append(f"Objective: {goal.objective}")
        out.append(f"Deliverable: {goal.deliverable}")
        out.append("Success criteria:")
        out += [f"  {c.id}: {c.description} [check: {c.verification}]" for c in goal.success_criteria]
        for label, items in (("Constraints", goal.constraints), ("Assumptions", goal.assumptions), ("Risk notes", goal.risk_notes), ("Blocking questions", goal.blocking_questions)):
            if items:
                out.append(f"{label}: " + " | ".join(items))
        out.append(f"Batch: {'yes' if goal.batch else 'no'}")
        out.append("")
    if state.plan:
        out.append("PLAN")
        out += [f"  [{s.status}] {s.id}: {s.description}" + (f" ({s.note})" if s.note else "") for s in state.plan]
        out.append("")
    if state.facts:
        out.append("TASK FACTS")
        out += [f"  {k} = {_json(v)}" for k, v in state.facts.items()]
        out.append("")
    if state.decisions:
        out.append("APPROVAL DECISIONS")
        for d in state.decisions:
            line = f"  {d['approval_id']} {d['decision']} by {d['by']}" + (f" ('{d['note']}')" if d.get("note") else "") + f" — {d.get('summary', '')}"
            if d["decision"] == "approved":
                line += " → submit the identical values once more; the gateway will let it through."
            else:
                line += " → do not post it; report the rejection and its reason."
            out.append(line)
        out.append("")
    if state.answers:
        out.append("HUMAN ANSWERS")
        out += [f"  {a['question_id']} \"{a['question']}\" → \"{a['answer']}\" ({a['by']})" for a in state.answers]
        out.append("")
    recent_notes = [n for n in state.notes if n.get("type") not in ("retrieval",)][-6:]
    if recent_notes:
        out.append("NOTES FROM THE SYSTEM")
        out += [f"  - {n['text']}" for n in recent_notes]
        out.append("")
    return "\n".join(out).rstrip()


def _compact(record: StepRecord) -> str:
    args = {k: v for k, v in record.arguments.items() if k != "why"}
    shown = " ".join(f"{v}" if k in ("ref", "url", "connector_id", "key", "path", "query", "text") else f"{k}={json.dumps(v, ensure_ascii=False)[:60]}" for k, v in args.items())
    status = "ok" if record.observation.ok else "FAILED"
    return f"#{record.step} {record.tool} {shown[:120]} (why: {record.why[:100]}) → {record.observation.summary[:160]} ({status})"


def _trusted_lines(obs: Observation) -> list[str]:
    lines = []
    for event in obs.policy_events:
        lines.append(f"  POLICY [{event.type}{':' + event.status if event.status else ''}] {event.message}")
    for note in obs.meta.get("notes", []):
        lines.append(f"  NOTE: {note}")
    for flag in obs.flags:
        lines.append(f"  FLAG [{flag.get('type')}] {flag.get('excerpt', '')[:200]}")
    if obs.error:
        lines.append(f"  ERROR: {obs.error}")
    if obs.artifacts:
        lines.append(f"  ARTIFACTS: {', '.join(obs.artifacts)}")
    return lines


def render_observation(record: StepRecord, content_chars: int, include_snapshot: bool) -> str:
    obs = record.observation
    args = {k: v for k, v in record.arguments.items() if k != "why"}
    head = f"#{record.step} {record.tool} {json.dumps(args, ensure_ascii=False)[:300]} (why: {record.why})"
    lines = [head, f"  → {'ok' if obs.ok else 'FAILED'}: {obs.summary}"] + _trusted_lines(obs)
    is_snapshot = record.tool in BROWSER_TOOLS
    if obs.content and (include_snapshot or not is_snapshot):
        content = obs.content if len(obs.content) <= content_chars else obs.content[:content_chars] + "\n…(truncated)"
        lines.append(wrap(content, obs.source) if obs.untrusted else content)
    return "\n".join(lines)


def history_block(state: TaskState, full_steps: int) -> str:
    previous = state.history[:-1]
    if not previous:
        return "(no earlier steps)"
    cut = max(0, len(previous) - (full_steps - 1))
    lines = [_compact(r) for r in previous[:cut]]
    lines += [render_observation(r, RECENT_CONTENT_CHARS, include_snapshot=False) for r in previous[cut:]]
    return "\n".join(lines)


def latest_block(state: TaskState) -> str:
    if not state.history:
        return "(none yet — this is the first step)"
    return render_observation(state.history[-1], LATEST_CONTENT_CHARS, include_snapshot=True)


def budget_line(state: TaskState, settings: Settings) -> str:
    max_steps = state.max_steps or (settings.max_steps_batch if state.goal and state.goal.batch else settings.max_steps)
    tokens = state.tokens_in + state.tokens_out
    return (
        f"Step {state.next_step} of {max_steps} · tokens {tokens // 1000}k of {settings.task_token_budget // 1000}k"
        f" · active time {state.active_minutes():.1f} of {settings.task_wallclock_min} min"
    )


def build(state: TaskState, company: Company, retriever: Retriever, settings: Settings, cache: ContextCache) -> tuple[list[Message], Retrieved, bool]:
    """Returns (messages, retrieved, refreshed) — refreshed is True when the company context was re-retrieved."""
    key = (state.plan_version, state.step_count // 10)
    refreshed = cache.key != key
    if refreshed:
        cache.retrieved = retriever.retrieve(_retrieval_query(state), k_handbook=4, k_facts=3, k_episodes=2, expand=True)
        cache.text = company_context(company, cache.retrieved)
        cache.key = key
    system = system_prompt(company) + "\n\n=== COMPANY CONTEXT ===\n" + cache.text
    user = render(
        load_prompt("step"),
        task_state_block=task_state_block(state),
        history_block=history_block(state, settings.history_full_steps),
        latest_observation=latest_block(state),
        budget_line=budget_line(state, settings),
    )
    return [Message("system", system), Message("user", user)], cache.retrieved, refreshed

"""Evidence-rich task report: runs/<task>/report.md + report.json (plan §11.9).

Sections: outcome, request, what I did (with each action's `why`), results,
verification (criterion -> verdict -> evidence), needs your attention, flags,
evidence (screenshots, record refs, files), company knowledge used, stats.
"""

from __future__ import annotations

import json
from typing import Any

from karya.db import loads, now_iso, parse_iso
from karya.gateway import approvals, intents
from karya.memory import store
from karya.runtime import ledger
from karya.runtime.ledger import StepRecord, TaskState
from karya.runtime.models import FinishArgs, VerificationResult

STATUS_LABEL = {"succeeded": "Succeeded", "partial": "Partial", "escalated": "Escalated", "failed": "Failed", "cancelled": "Cancelled"}
KEY_TOOLS = ("files_extract", "files_read", "api_get", "ask_human", "files_write")


def _duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


def key_actions(history: list[StepRecord], limit: int = 18) -> list[dict[str, Any]]:
    """Writes and important reads, each with its why."""
    picked = []
    for record in history:
        obs = record.observation
        important = record.tool in KEY_TOOLS or any(e.type != "approved_use" for e in obs.policy_events) or any(a.startswith("downloads/") for a in obs.artifacts) or record.tool == "finish"
        if important:
            picked.append({"step": record.step, "tool": record.tool, "summary": obs.summary, "why": record.why, "ok": obs.ok, "events": [e.message for e in obs.policy_events]})
    if len(picked) > limit:
        writes = [p for p in picked if p["events"]]
        others = [p for p in picked if not p["events"]]
        picked = sorted(writes + others[: max(0, limit - len(writes))], key=lambda p: p["step"])
    return picked


def collect_flags(state: TaskState, finish: FinishArgs) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = [{"type": f.type, "detail": f.detail, "evidence": f.evidence, "source": "worker"} for f in finish.flags]
    seen = {f["detail"][:80] for f in flags}
    for record in state.history:
        for flag in record.observation.flags:
            excerpt = flag.get("excerpt", "")
            if excerpt[:80] in seen or any(excerpt[:60] in f["detail"] for f in flags):
                continue
            seen.add(excerpt[:80])
            flags.append({"type": flag.get("type", "other"), "detail": excerpt, "evidence": [flag.get("source") or f"step {record.step}"], "source": f"detector ({flag.get('detector', 'heuristic')}) at step {record.step}"})
    return flags


def stats(state: TaskState, conn: Any) -> dict[str, Any]:
    events = [e for r in state.history for e in r.observation.policy_events]
    task_intents = intents.for_task(conn, state.task_id)
    return {
        "steps": state.step_count,
        "tokens_in": state.tokens_in,
        "tokens_out": state.tokens_out,
        "tool_errors": sum(1 for r in state.history if not r.observation.ok),
        "get_retries": sum(int(r.observation.meta.get("retries", 0) or 0) for r in state.history),
        "relogins": sum(1 for r in state.history if r.observation.meta.get("relogin")),
        "reconciliations": sum(1 for i in task_intents if i["reconciled"]),
        "policy_blocks": sum(1 for e in events if e.type in ("denied", "blocked_host", "read_only_block")),
        "duplicates_prevented": sum(1 for e in events if e.type == "duplicate_prevented"),
        "writes_committed": sum(1 for i in task_intents if i["status"] == "committed"),
        "questions_asked": conn.execute("SELECT COUNT(*) FROM questions WHERE task_id = ?", (state.task_id,)).fetchone()[0],
        "approvals": len(approvals.for_task(conn, state.task_id)),
        "verifier_rounds": state.verify_rounds,
        "active_minutes": round(state.active_minutes(), 2),
    }


def knowledge_used(state: TaskState, conn: Any) -> list[dict[str, str]]:
    """Learned facts that were in the model's context during this task (cited deterministically)."""
    ids: list[str] = []
    for note in state.notes_of("retrieval"):
        ids += [i for i in note.get("ids", []) if i.startswith("F-")]
    rows = conn.execute("SELECT payload_json FROM steps WHERE task_id = ? AND kind = 'intake'", (state.task_id,)).fetchall()
    for row in rows:
        ids += [i for i in loads(row["payload_json"], {}).get("retrieved", []) if i.startswith("F-")]
    out = []
    for fact_id in dict.fromkeys(ids):
        fact = store.get_fact(conn, fact_id)
        if fact:
            out.append({"id": fact.id, "statement": fact.statement, "provenance": fact.provenance()})
    return out


def build(state: TaskState, finish: FinishArgs, result: VerificationResult, status: str, ctx: Any) -> dict[str, Any]:
    conn = ctx.conn
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (state.task_id,)).fetchone()
    elapsed = (parse_iso(now_iso()) - parse_iso(task["created_at"])).total_seconds()
    criteria = {c.id: c.description for c in state.goal.success_criteria} if state.goal else {}
    task_intents = intents.for_task(conn, state.task_id)
    attention = list(finish.needs_attention)
    for apr in approvals.for_task(conn, state.task_id):
        if apr["status"] == "pending":
            attention.append(f"Approval {apr['id']} is still pending: {apr['summary']}")
        elif apr["status"] == "rejected":
            attention.append(f"Approval {apr['id']} was rejected by {apr['decided_by']}: {apr['reason']}")
    for q in conn.execute("SELECT * FROM questions WHERE task_id = ? AND answer IS NULL", (state.task_id,)):
        attention.append(f"Question {q['id']} is unanswered: {q['question']}")
    attention += [f"{i.item}: {i.outcome}{' — ' + i.note if i.note else ''}" for i in finish.items if i.outcome != "done"]
    screenshots: list[str] = []
    for claim in finish.claims:
        screenshots += [e for e in claim.evidence if e.startswith("screens/")]
    for record in state.history:  # screenshots of writes, holds and blocks
        if record.observation.policy_events:
            screenshots += [a for a in record.observation.artifacts if a.startswith("screens/")]
    last_shot = next((a for r in reversed(state.history) for a in r.observation.artifacts if a.startswith("screens/")), None)
    if last_shot:
        screenshots.append(last_shot)
    downloads_dir = ctx.settings.runs_dir / state.task_id / "downloads"
    return {
        "task_id": state.task_id,
        "status": status,
        "finish_status": finish.status,
        "summary": finish.summary,
        "request": state.request,
        "requester": state.requester,
        "created_at": task["created_at"],
        "finished_at": now_iso(),
        "elapsed_seconds": round(elapsed, 1),
        "goal": state.goal.model_dump() if state.goal else None,
        "what_i_did": key_actions(state.history),
        "items": [i.model_dump() for i in finish.items],
        "verification": {
            "overall": result.overall,
            "notes": result.notes,
            "verdicts": [{**v.model_dump(), "criterion": criteria.get(v.criterion_id, "")} for v in result.verdicts],
            "system_evidence": state.verifications[-1].get("system_evidence", []) if state.verifications else [],
        },
        "claims": [c.model_dump() for c in finish.claims],
        "needs_attention": attention,
        "flags": collect_flags(state, finish),
        "evidence": {
            "screenshots": list(dict.fromkeys(screenshots)),
            "records": [f"{i['connector']}:{i['record_ref']}" for i in task_intents if i["status"] == "committed" and i["record_ref"]],
            "files": sorted(f"downloads/{p.name}" for p in downloads_dir.glob("*")) if downloads_dir.exists() else [],
        },
        "knowledge_used": knowledge_used(state, conn),
        "stats": stats(state, conn),
    }


def render_markdown(data: dict[str, Any]) -> str:
    s = data["stats"]
    label = STATUS_LABEL.get(data["status"], data["status"])
    lines = [
        f"# {data['task_id']} · {label} · {_duration(s['active_minutes'] * 60)} of work",
        "",
        f"**Request:** {data['request']}",
        f"**Outcome:** {data['summary']}",
        "",
        "## What I did",
    ]
    for i, action in enumerate(data["what_i_did"], 1):
        events = f" — {' / '.join(action['events'])}" if action["events"] else ""
        lines.append(f"{i}. Step {action['step']} `{action['tool']}`: {action['summary']}{events} (why: {action['why']})")
    if not data["what_i_did"]:
        lines.append("Nothing notable.")
    if data["items"]:
        lines += ["", "## Results", "| Item | Outcome | Ref | Note |", "| --- | --- | --- | --- |"]
        lines += [f"| {i['item']} | {i['outcome']} | {i['ref'] or '—'} | {i['note']} |" for i in data["items"]]
    v = data["verification"]
    lines += ["", f"## Verification (independent, read-only): {v['overall']}", "| Criterion | Verdict | Evidence | Note |", "| --- | --- | --- | --- |"]
    for verdict in v["verdicts"]:
        lines.append(f"| {verdict['criterion_id']} {verdict['criterion']} | {verdict['verdict']} | {', '.join(verdict['evidence']) or '—'} | {verdict['note']} |")
    if v["notes"]:
        lines += ["", f"Verifier notes: {v['notes']}"]
    if v["system_evidence"]:
        lines += ["", "System evidence (code lookups):"] + [f"- {e}" for e in v["system_evidence"]]
    lines += ["", "## Needs your attention"] + ([f"- {a}" for a in data["needs_attention"]] or ["Nothing."])
    lines += ["", "## Flags"] + ([f"- ⚠ **{f['type']}**: {f['detail']} ({', '.join(f['evidence'])})" for f in data["flags"]] or ["None."])
    ev = data["evidence"]
    lines += [
        "",
        "## Evidence",
        f"- Screenshots: {', '.join(ev['screenshots']) or '—'}",
        f"- Records: {', '.join(ev['records']) or '—'}",
        f"- Files: {', '.join(ev['files']) or '—'}",
    ]
    if data["knowledge_used"]:
        lines += ["", "## Company knowledge used"] + [f"- {k['id']}: {k['statement']} ({k['provenance']})" for k in data["knowledge_used"]]
    lines += [
        "",
        "## Stats",
        f"{s['steps']} steps · {s['tokens_in'] // 1000}k tokens in / {s['tokens_out'] // 1000}k out · {s['get_retries']} GET retries · "
        f"{s['relogins']} re-logins · {s['reconciliations']} reconciliations · {s['policy_blocks']} policy blocks · "
        f"{s['duplicates_prevented']} duplicates prevented · {s['questions_asked']} questions · {s['verifier_rounds']} verifier round(s) · "
        f"{_duration(data['elapsed_seconds'])} elapsed",
    ]
    return "\n".join(lines) + "\n"


def write(state: TaskState, finish: FinishArgs, result: VerificationResult, status: str, ctx: Any) -> dict[str, Any]:
    data = build(state, finish, result, status, ctx)
    run_dir = ctx.settings.runs_dir / state.task_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.json").write_text(json.dumps(data, indent=2, default=str, ensure_ascii=False), encoding="utf-8")
    (run_dir / "report.md").write_text(render_markdown(data), encoding="utf-8")
    ledger.append(ctx.conn, state.task_id, "report", {"status": status, "summary": finish.summary, "path": f"runs/{state.task_id}/report.md"})
    decisions = [f"{d['approval_id']} {d['decision']} by {d['by']}" + (f" ({d['note']})" if d.get("note") else "") for d in state.decisions]
    decisions += [f"{a['question_id']}: {a['answer']} ({a['by']})" for a in state.answers]
    decisions += [f"flag {f['type']}: {f['detail'][:120]}" for f in data["flags"]]
    store.save_episode(ctx.conn, state.task_id, state.request, status, finish.summary, decisions)
    return data

"""The agent loop (plan §11.5). Plain Python, no framework (DECISIONS.md D1).

  Goal -> Understand -> Plan -> Execute -> Observe -> Adapt -> Verify -> Complete

Each iteration: guard (budgets, stuck detection) -> build context from the
ledger -> ONE model call -> ONE tool call (with a `why`) -> observation ->
ledger checkpoint. Approvals and questions suspend the task; `finish` hands
over to the independent verifier, whose verdict decides the final status.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from pydantic import ValidationError

from karya.company import Company
from karya.config import Settings
from karya.db import audit
from karya.gateway.gateway import Gateway
from karya.gateway.intents import ReconcileLookup
from karya.gateway.secrets import Secrets
from karya.llm.base import LLMClient, ToolCall, new_call_id
from karya.memory.retrieval import Retriever
from karya.runtime import context, intake, ledger, report, stuck, verifier
from karya.runtime.ledger import TaskState
from karya.runtime.models import FinishArgs, Observation, PlanStep, VerificationResult
from karya.tools import registry
from karya.tools.base import ToolSession
from karya.worker import queue

FINAL_STATUS = {"completed": "succeeded", "partial": "partial", "escalated": "escalated", "cannot_complete": "failed"}
MAX_VERIFY_ROUNDS = 2
MAX_NO_TOOL_REPLIES = 3


@dataclass
class RunContext:
    """What one run of a task needs. Tests inject a ScriptedLLM, a lookup or hooks."""

    settings: Settings
    company: Company
    llm: LLMClient
    conn: sqlite3.Connection
    worker_id: str = "foreground"
    stop: threading.Event = field(default_factory=threading.Event)    # worker shutdown (Ctrl-C)
    abort: threading.Event = field(default_factory=threading.Event)   # this task only (lease lost)
    hooks: dict[str, Callable[..., Any]] = field(default_factory=dict)  # e.g. "before_verify"
    lookup: ReconcileLookup | None = None
    verifier_llm: LLMClient | None = None  # defaults to llm


@dataclass
class LoopResult:
    outcome: Literal["done", "suspended", "interrupted"]
    status: str          # task status to set: succeeded | partial | escalated | failed | waiting_* | queued
    detail: str = ""
    result: dict[str, Any] | None = None


def _note(conn: sqlite3.Connection, task_id: str, kind: str, text: str, **extra: Any) -> None:
    ledger.append(conn, task_id, "system", {"type": kind, "text": text, **extra})


def resume_note(state: TaskState, reconcile_messages: list[str]) -> str:
    parts = ["Resumed. The browser was restarted and is NOT logged in to any system."]
    if state.dangling:
        parts.append(
            f"The previous run stopped during step {state.dangling['step']} ({state.dangling.get('tool')}); "
            "re-check the page before repeating it."
        )
    parts += reconcile_messages
    for decision in state.decisions:
        parts.append(f"Approval {decision['approval_id']} was {decision['decision']} by {decision['by']}" + (f": {decision['note']}" if decision.get("note") else "."))
    for answer in state.answers:
        parts.append(f"{answer['by']} answered {answer['question_id']}: \"{answer['answer']}\"")
    return " ".join(parts)


def run_task(task_id: str, ctx: RunContext) -> LoopResult:
    conn, company, settings = ctx.conn, ctx.company, ctx.settings
    state = ledger.load_state(conn, task_id)
    retriever = Retriever(company, conn)

    if state.goal is None:
        goal, plan, usage, retrieved = intake.run(ctx.llm, company, retriever, state.request, state.requester)
        queue.add_tokens(conn, task_id, usage)
        ledger.append(conn, task_id, "intake", {"goal_spec": goal.model_dump(), "retrieved": retrieved.ids(), "usage": vars(usage)})
        ledger.append(conn, task_id, "plan", {"steps": [s.model_dump() for s in plan], "source": "intake"})
        audit(conn, "agent", "intake", {"objective": goal.objective, "criteria": [c.id for c in goal.success_criteria]}, task_id)
        state = ledger.load_state(conn, task_id)

    secrets = Secrets(company.connectors)
    gateway = Gateway(task_id, company, conn, secrets, mode="act", lookup=ctx.lookup)
    session = ToolSession(task_id, company, conn, ctx.llm, gateway, settings=settings, retriever=retriever, requester=state.requester)
    cache = context.ContextCache()
    try:
        if state.has_started:
            # Crash-safe resume: settle writes that were in flight before taking any new step.
            events = gateway.reconcile_unresolved()
            _note(conn, task_id, "resume", resume_note(state, [e.message for e in events]))
            state = ledger.load_state(conn, task_id)

        no_tool_replies = 0
        while True:
            if ctx.stop.is_set() or ctx.abort.is_set():
                return LoopResult("interrupted", "queued", "worker stopping; task returned to the queue")

            guard = stuck.check(state, settings)
            if guard.action == "escalate":
                return _forced_finish(state, ctx, session, guard.reason)
            if guard.action == "nudge":
                _note(conn, task_id, "nudge", guard.reason, step=state.step_count)
                state = ledger.load_state(conn, task_id)

            messages, retrieved, refreshed = context.build(state, company, retriever, settings, cache)
            if refreshed:
                _note(conn, task_id, "retrieval", "company context refreshed", ids=retrieved.ids())
            tool_choice: Any = "required"
            if state.goal and state.goal.blocking_questions and not state.answers and not state.history:
                tool_choice = {"name": "ask_human"}  # intake judged investigation cannot answer this
            response = ctx.llm.chat(messages, registry.specs("worker"), tool_choice=tool_choice)
            queue.add_tokens(conn, task_id, response.usage)

            if not response.tool_calls:
                no_tool_replies += 1
                if no_tool_replies >= MAX_NO_TOOL_REPLIES:
                    return _forced_finish(state, ctx, session, "The model repeatedly replied without calling a tool.")
                _note(conn, task_id, "no_tool_call", "Your last reply did not call a tool. Call exactly one tool.")
                state = ledger.load_state(conn, task_id)
                continue
            no_tool_replies = 0

            call = response.tool_calls[0]
            step = state.next_step
            ledger.append(conn, task_id, "llm", {
                "step": step, "tool": call.name, "arguments": call.arguments, "text": response.text[:2000],
                "usage": vars(response.usage), "model": response.model, "ignored_calls": len(response.tool_calls) - 1,
            })

            if call.name == "finish":
                result = _handle_finish(state, ctx, session, call, step)
                if result is not None:
                    return result
                state = ledger.load_state(conn, task_id)
                continue

            observation = session.execute(call, step)
            if len(response.tool_calls) > 1:
                notes = observation.meta.get("notes", []) + [f"Only the first of {len(response.tool_calls)} tool calls was executed; one tool per step."]
                observation = observation.model_copy(update={"meta": {**observation.meta, "notes": notes}})
            if session.tool_usage.input_tokens or session.tool_usage.output_tokens:
                queue.add_tokens(conn, task_id, session.tool_usage)
                session.tool_usage = type(session.tool_usage)()
            ledger.append(conn, task_id, "observation", {"step": step, "tool": call.name, "observation": observation.model_dump()})
            queue.set_step_count(conn, task_id, step)

            if call.name == "update_plan" and observation.ok:
                steps = [PlanStep(id=s.get("id") or f"p{i}", description=s["description"], status=s.get("status", "pending"), note=s.get("note")) for i, s in enumerate(call.arguments.get("steps", []), 1)]
                ledger.append(conn, task_id, "plan", {"steps": [s.model_dump() for s in steps], "source": "update_plan", "step": step})

            held = [e for e in observation.policy_events if e.type == "approval_required"]
            if held:
                return LoopResult("suspended", "waiting_approval", f"waiting for approval {held[0].approval_id}")
            if call.name == "ask_human" and observation.ok:
                return LoopResult("suspended", "waiting_human", f"waiting for an answer to {observation.meta.get('question_id')}")
            state = ledger.load_state(conn, task_id)
    finally:
        session.close()


def _handle_finish(state: TaskState, ctx: RunContext, session: ToolSession, call: ToolCall, step: int) -> LoopResult | None:
    conn, task_id = ctx.conn, state.task_id
    try:
        finish = FinishArgs.model_validate({k: v for k, v in call.arguments.items() if k != "why"})
    except ValidationError as exc:
        obs = Observation(ok=False, summary="finish: invalid arguments", error=str(exc)[:800])
        ledger.append(conn, task_id, "observation", {"step": step, "tool": "finish", "observation": obs.model_dump()})
        return None
    obs = Observation(ok=True, summary=f"finish({finish.status}) — handing over to the independent verifier")
    ledger.append(conn, task_id, "observation", {"step": step, "tool": "finish", "observation": obs.model_dump()})
    queue.set_step_count(conn, task_id, step)
    return _verify_and_close(ledger.load_state(conn, task_id), ctx, finish)


def _verify_and_close(state: TaskState, ctx: RunContext, finish: FinishArgs) -> LoopResult | None:
    conn, task_id = ctx.conn, state.task_id
    if "before_verify" in ctx.hooks:
        ctx.hooks["before_verify"](task_id)
    result, evidence = verifier.verify(state, finish, ctx)
    ledger.append(conn, task_id, "verify", {"round": state.verify_rounds + 1, "finish": finish.model_dump(), "result": result.model_dump(), "system_evidence": evidence})
    audit(conn, "verifier", "verified", {"overall": result.overall, "verdicts": [v.model_dump() for v in result.verdicts]}, task_id)
    accepted = finish.status in ("partial", "cannot_complete") or result.overall == "pass"
    if accepted:
        status = FINAL_STATUS[finish.status]
    elif state.verify_rounds + 1 >= MAX_VERIFY_ROUNDS:
        status = "partial" if any(v.verdict == "pass" for v in result.verdicts) else "failed"
    else:
        failing = "; ".join(f"{v.criterion_id} {v.verdict}: {v.note}" for v in result.verdicts if v.verdict != "pass")
        _note(conn, task_id, "verifier_feedback", f"The independent verifier did not accept the finish ({failing}). {result.notes} Fix what is wrong, or finish with an honest status.")
        return None
    final = ledger.load_state(conn, task_id)
    data = report.write(final, finish, result, status, ctx)
    return LoopResult("done", status, finish.summary, result=data)


def _forced_finish(state: TaskState, ctx: RunContext, session: ToolSession, reason: str) -> LoopResult:
    """Budget/stuck escalation: still verified and reported, never silently dropped."""
    conn, task_id = ctx.conn, state.task_id
    step = state.next_step
    finish = FinishArgs(status="escalated", summary=f"Stopped and escalated: {reason}", needs_attention=[reason])
    arguments = {"why": "Runtime guard: " + reason[:150], **finish.model_dump()}
    ledger.append(conn, task_id, "tool_call", {"step": step, "tool": "finish", "arguments": arguments, "actor": "system", "id": new_call_id()})
    obs = Observation(ok=True, summary=f"forced finish(escalated): {reason}")
    ledger.append(conn, task_id, "observation", {"step": step, "tool": "finish", "observation": obs.model_dump()})
    audit(conn, "system", "forced_escalation", {"reason": reason}, task_id)
    result = _verify_and_close(ledger.load_state(conn, task_id), ctx, finish)
    if result is None:  # the verifier rejected the escalation; one forced finish only
        final = ledger.load_state(conn, task_id)
        verification = VerificationResult.model_validate(final.verifications[-1]["result"])
        data = report.write(final, finish, verification, "failed", ctx)
        return LoopResult("done", "failed", reason, result=data)
    return result

"""Phase 6: reliability under chaos — armed faults against the live sandbox, ScriptedLLM agent.

Each test arms a deterministic fault and checks that the runtime recovers without
duplicates: unknown-outcome writes are reconciled, failed writes are safely
retried, lost sessions are re-established, page errors are retried, and an agent
that repeats itself is nudged and then escalated.
"""

from __future__ import annotations

import json

import pytest

from karya.llm.scripted import ScriptedLLM
from karya.runtime import ledger
from karya.worker import queue
from tests import scripted_flows as flows
from tests.test_e2e_scripted import REQUEST, erp_invoices, make_ctx, mail_message, run_until_rest

pytestmark = pytest.mark.browser


def erp_chaos():
    from sandbox.erp.app import chaos

    return chaos


def run_happy(live_sandbox, base, steps=None):
    llm = flows.happy_llm(live_sandbox, base)
    if steps is not None:
        llm.steps.clear()
        llm.steps.extend(steps)
    ctx = make_ctx(llm)
    task_id, result = run_until_rest(ctx)
    return ctx, task_id, result, ledger.load_state(ctx.conn, task_id)


def events_of(state, kind: str):
    return [e for r in state.history for e in r.observation.policy_events if e.type == kind]


def test_commit_then_504_is_reconciled_not_resubmitted(live_sandbox, fresh_sandbox):
    erp_chaos().arm("commit_then_504", 1, "/invoices")
    ctx, task_id, result, state = run_happy(live_sandbox, fresh_sandbox)
    assert result.status == "succeeded"
    assert len(erp_invoices(live_sandbox, vendor_id="V001", invoice_number="AI/2026/0917")) == 1
    outcomes = events_of(state, "write_outcome")
    reconciled = [e for e in outcomes if "HTTP 504" in e.message]
    assert reconciled and "but the record exists (ERP-INV-000104)" in reconciled[0].message and "do not resubmit" in reconciled[0].message
    intent = ctx.conn.execute("SELECT * FROM intents WHERE task_id = ? AND action = 'create_invoice'", (task_id,)).fetchall()
    assert len(intent) == 1 and intent[0]["status"] == "committed" and intent[0]["reconciled"] == 1 and intent[0]["http_status"] == 504
    report = json.loads((ctx.settings.runs_dir / task_id / "report.json").read_text())
    assert report["stats"]["reconciliations"] == 1


def test_commit_then_504_then_a_resubmit_is_blocked(live_sandbox, fresh_sandbox):
    base = fresh_sandbox
    erp_chaos().arm("commit_then_504", 1, "/invoices")
    steps = flows.happy_path_steps(live_sandbox, base)
    resubmit = steps[7:16]  # a model that ignores the reconciliation message and posts again
    ctx, task_id, result, state = run_happy(live_sandbox, base, steps[:16] + resubmit + steps[16:])
    assert result.status == "succeeded"
    assert len(erp_invoices(live_sandbox, vendor_id="V001", invoice_number="AI/2026/0917")) == 1
    blocked = events_of(state, "duplicate_prevented")
    assert blocked and "Already done in this task (ERP-INV-000104)" in blocked[0].message


def test_write_conflict_409_fails_cleanly_and_resubmit_succeeds(live_sandbox, fresh_sandbox):
    base = fresh_sandbox
    erp_chaos().arm("write_conflict_409", 1, "/invoices")
    steps = flows.happy_path_steps(live_sandbox, base)
    retry = steps[7:16]  # reopen the form, refill, resubmit
    ctx, task_id, result, state = run_happy(live_sandbox, base, steps[:16] + retry + steps[16:])
    assert result.status == "succeeded"
    assert len(erp_invoices(live_sandbox, vendor_id="V001", invoice_number="AI/2026/0917")) == 1
    rows = ctx.conn.execute("SELECT status, http_status FROM intents WHERE task_id = ? AND action = 'create_invoice' ORDER BY id", (task_id,)).fetchall()
    assert [(r["status"], r["http_status"]) for r in rows] == [("failed", 409), ("committed", 303)]
    failed = [e for e in events_of(state, "write_outcome") if e.status == "failed"]
    assert failed and "HTTP 409" in failed[0].message and "Nothing was saved" in failed[0].message


def test_session_expired_on_form_page_relogin_and_complete(live_sandbox, fresh_sandbox):
    base = fresh_sandbox
    erp_chaos().arm("session_expired", 1, "/invoices/new")
    steps = flows.happy_path_steps(live_sandbox, base)
    relogin = flows.step("browser_login", "The ERP logged me out; log in again (I land back on the form).", connector_id="erp")
    ctx, task_id, result, state = run_happy(live_sandbox, base, steps[:8] + [relogin] + steps[8:])
    assert result.status == "succeeded"
    assert len(erp_invoices(live_sandbox, vendor_id="V001", invoice_number="AI/2026/0917")) == 1
    hinted = [r for r in state.history if any("You appear to be logged out of erp" in n for n in r.observation.meta.get("notes", []))]
    assert hinted, "the agent was told it is logged out"
    login = next(r for r in state.history if r.step == 9)
    assert login.tool == "browser_login" and login.observation.meta.get("relogin") is True
    assert "/invoices/new" in login.observation.summary  # login kept ?next= and returned to the form
    report = json.loads((ctx.settings.runs_dir / task_id / "report.json").read_text())
    assert report["stats"]["relogins"] == 1


def test_session_expired_on_submit_loses_the_post_and_says_so(live_sandbox, fresh_sandbox):
    base = fresh_sandbox
    erp_chaos().arm("session_expired", 1, "/invoices")
    steps = flows.happy_path_steps(live_sandbox, base)
    redo = [flows.step("browser_login", "Session expired; log in again.", connector_id="erp")] + steps[7:16]
    ctx, task_id, result, state = run_happy(live_sandbox, base, steps[:16] + redo + steps[16:])
    assert result.status == "succeeded"
    assert len(erp_invoices(live_sandbox, vendor_id="V001", invoice_number="AI/2026/0917")) == 1
    lost = [e for e in events_of(state, "write_outcome") if e.status == "failed"]
    assert lost and "the form was NOT submitted" in lost[0].message


def test_http_500_page_loads_are_retried(live_sandbox, fresh_sandbox):
    erp_chaos().arm("http_500", 2, "/invoices/new")
    ctx, task_id, result, state = run_happy(live_sandbox, fresh_sandbox)
    assert result.status == "succeeded"
    opened = next(r for r in state.history if r.tool == "browser_open" and "/invoices/new" in r.arguments.get("url", ""))
    assert opened.observation.ok and opened.observation.meta["retries"] == 2
    assert any("Retried 2x" in n for n in opened.observation.meta["notes"])


def test_http_500_on_the_api_is_retried(live_sandbox, fresh_sandbox):
    erp_chaos().arm("http_500", 2, "/api/*")
    ctx, task_id, result, state = run_happy(live_sandbox, fresh_sandbox)
    assert result.status == "succeeded"
    api_steps = [r for r in state.history if r.tool == "api_get"]
    assert api_steps[0].observation.ok and api_steps[0].observation.meta["retries"] == 2


def test_label_drift_does_not_break_the_agent(live_sandbox, fresh_sandbox):
    erp_chaos().arm("label_drift", 20)
    from sandbox.mail.app import chaos as mail_chaos

    mail_chaos.arm("label_drift", 20)
    ctx, task_id, result, state = run_happy(live_sandbox, fresh_sandbox)
    assert result.status == "succeeded"
    assert "Processed" in mail_message(live_sandbox, 2)["labels"]


def test_identical_actions_nudge_then_escalate(live_sandbox, fresh_sandbox):
    same = flows.step("memory_search", "Look for the procedure again.", query="invoice duplicates")
    ctx = make_ctx(ScriptedLLM([same] * 8, forced={"submit_goal_spec": flows.HAPPY_GOAL}))
    task_id, result = run_until_rest(ctx)
    state = ledger.load_state(ctx.conn, task_id)
    nudges = state.notes_of("nudge")
    assert len(nudges) == 1 and "repeating an action" in nudges[0]["text"]
    assert result.status == "escalated"
    assert len(state.history) == 5  # 3 repeats -> nudge -> 1 more repeat -> escalate (forced finish = step 5)
    assert state.history[-1].tool == "finish" and "Still repeating" in state.history[-1].observation.summary


def test_same_denied_action_twice_escalates(live_sandbox, fresh_sandbox):
    erp = live_sandbox["erp"]
    def bank_change():
        return [
            flows.step("browser_open", "Open the vendor.", url=f"{erp}/vendors/V004"),
            lambda m: flows.step("browser_fill", "Account.", ref=flows.ref(m, "textbox", name="account_number"), value="50200099887766"),
            lambda m: flows.step("browser_fill", "IFSC.", ref=flows.ref(m, "textbox", name="ifsc"), value="HDFC0001234"),
            lambda m: flows.step("browser_fill", "Reason.", ref=flows.ref(m, "textarea", name="reason"), value="per vendor note"),
            lambda m: flows.step("browser_click", "Submit.", ref=flows.submit_button(m)),
        ]
    steps = [flows.step("browser_login", "Log in.", connector_id="erp")] + bank_change() + bank_change()
    ctx = make_ctx(ScriptedLLM(steps, forced={"submit_goal_spec": flows.HAPPY_GOAL}))
    task_id, result = run_until_rest(ctx)
    state = ledger.load_state(ctx.conn, task_id)
    assert result.status == "escalated"
    assert "Policy AP-002 blocks this" in state.history[-1].observation.summary
    assert len(events_of(state, "denied")) == 2


def test_step_budget_escalates(live_sandbox, fresh_sandbox):
    steps = [flows.step("memory_search", f"Search {i}.", query=f"topic {i}") for i in range(10)]
    ctx = make_ctx(ScriptedLLM(steps, forced={"submit_goal_spec": flows.HAPPY_GOAL}))
    task_id = queue.enqueue(ctx.conn, REQUEST, "Arjun Rao", max_steps=4)
    queue.claim(ctx.conn, ctx.worker_id, task_id=task_id)
    from karya.worker.worker import process_task

    result = process_task(task_id, ctx)
    assert result.status == "escalated"
    assert "Step budget exhausted (4 of 4 steps)" in ledger.load_state(ctx.conn, task_id).history[-1].observation.summary

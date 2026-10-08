"""Phase 5: durable queue, worker, approvals and questions — through the real browser,
gateway and sandbox, driven by ScriptedLLM."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from karya.db import to_iso
from karya.gateway import approvals
from karya.llm.scripted import ScriptedLLM
from karya.runtime import ledger
from karya.worker import inbox, queue
from karya.worker.worker import process_task, run_worker
from tests import scripted_flows as flows
from tests.test_e2e_scripted import REQUEST, erp_invoices, mail_message, make_ctx

pytestmark = pytest.mark.browser
VERTEX_REQUEST = "Process the Vertex Semiconductors invoice in the AP mailbox."


class SimulatedCrash(BaseException):
    """Like kill -9: not an Exception, so nothing in the worker catches it or releases the lease."""


def finish_step(status: str, summary: str, **extra):
    return flows.step("finish", "Done with this task.", status=status, summary=summary, **extra)


def test_approval_flow_with_rbac_and_single_post(live_sandbox, fresh_sandbox):
    base = fresh_sandbox
    after_approval = flows.vertex_form_steps(live_sandbox, base) + [
        finish_step("completed", "Posted Vertex invoice VS-55120 for INR 1,84,500.00 after approval APR by Neha Kulkarni."),
    ]
    ctx = make_ctx(flows.vertex_llm(live_sandbox, base, after_hold=after_approval), worker_id="w1")
    task_id = queue.enqueue(ctx.conn, VERTEX_REQUEST, "Arjun Rao")

    assert run_worker(ctx, once=True) == 1
    assert queue.get(ctx.conn, task_id)["status"] == "waiting_approval"
    assert run_worker(ctx, once=True) == 0  # idle: nothing runnable while waiting
    apr = approvals.for_task(ctx.conn, task_id)[0]

    with pytest.raises(inbox.InboxError, match="Arjun Rao does not hold finance_manager"):
        inbox.decide(ctx.conn, ctx.company, apr["id"], True, "Arjun Rao")
    assert queue.get(ctx.conn, task_id)["status"] == "waiting_approval"

    inbox.decide(ctx.conn, ctx.company, apr["id"], True, "Neha Kulkarni", "PO 4502 received")
    assert queue.get(ctx.conn, task_id)["status"] == "queued"
    assert run_worker(ctx, once=True) == 1  # resumes: fresh browser, logs in, re-enters, posts once

    assert queue.get(ctx.conn, task_id)["status"] == "succeeded"
    records = erp_invoices(live_sandbox, invoice_number="VS-55120")
    assert len(records) == 1 and records[0]["amount"] == "184500.00"
    assert approvals.get(ctx.conn, apr["id"])["status"] == "consumed"
    state = ledger.load_state(ctx.conn, task_id)
    assert state.decisions[0]["by"] == "Neha Kulkarni"
    resume = state.notes_of("resume")
    assert resume and "Approval APR" in resume[0]["text"] and "approved by Neha Kulkarni" in resume[0]["text"]
    steps = [r.step for r in state.history]
    assert steps == sorted(set(steps)), "step numbers continue across the suspension"
    events = [e.type for r in state.history for e in r.observation.policy_events]
    assert events.count("approval_required") == 1 and "approved_use" in events


def test_rejection_escalates_without_posting(live_sandbox, fresh_sandbox):
    base = fresh_sandbox
    after_reject = [finish_step("escalated", "Approval for VS-55120 was rejected by Neha Kulkarni (PO not received); nothing was posted.", needs_attention=["Vertex invoice VS-55120 rejected: PO not received"])]
    ctx = make_ctx(flows.vertex_llm(live_sandbox, base, after_hold=after_reject), worker_id="w1")
    task_id = queue.enqueue(ctx.conn, VERTEX_REQUEST, "Arjun Rao")
    run_worker(ctx, once=True)
    apr = approvals.for_task(ctx.conn, task_id)[0]
    with pytest.raises(inbox.InboxError, match="needs a reason"):
        inbox.decide(ctx.conn, ctx.company, apr["id"], False, "Neha Kulkarni", "")
    inbox.decide(ctx.conn, ctx.company, apr["id"], False, "Neha Kulkarni", "PO not received")
    run_worker(ctx, once=True)
    assert queue.get(ctx.conn, task_id)["status"] == "escalated"
    assert erp_invoices(live_sandbox, invoice_number="VS-55120") == []
    report = (ctx.settings.runs_dir / task_id / "report.md").read_text()
    assert "PO not received" in report and "rejected" in report


def test_crash_lease_expiry_resume_without_duplicates(live_sandbox, fresh_sandbox):
    base = fresh_sandbox
    script = flows.happy_path_steps(live_sandbox, base)
    head, tail = script[:16], script[16:]  # step 16 submits the invoice; the tail labels the email and finishes

    def crash(messages):
        raise SimulatedCrash("worker killed right after the invoice was posted")

    llm1 = ScriptedLLM(head + [crash], forced=flows.happy_llm(live_sandbox, base).forced)
    ctx1 = make_ctx(llm1, worker_id="w1")
    task_id = queue.enqueue(ctx1.conn, REQUEST, "Arjun Rao")
    queue.claim(ctx1.conn, "w1", task_id=task_id)
    with pytest.raises(SimulatedCrash):
        process_task(task_id, ctx1)
    task = queue.get(ctx1.conn, task_id)
    assert task["status"] == "running" and task["lease_owner"] == "w1"  # nobody released it
    assert queue.claim(ctx1.conn, "w2", task_id=task_id) is None  # lease still valid

    ctx1.conn.execute("UPDATE tasks SET lease_expires_at = ? WHERE id = ?", (to_iso(datetime.now(timezone.utc) - timedelta(seconds=1)), task_id))
    resubmit = flows.happy_path_steps(live_sandbox, base)[6:16]  # a confused model posts the invoice again
    relogin_mail = flows.step("browser_login", "The browser restarted; log in to the mailbox again.", connector_id="mail")
    llm2 = ScriptedLLM(resubmit + [relogin_mail] + tail, forced=llm1.forced)
    ctx2 = make_ctx(llm2, worker_id="w2")
    assert queue.claim(ctx2.conn, "w2", task_id=task_id)["attempts"] == 2
    result = process_task(task_id, ctx2)
    assert result.status == "succeeded", result

    assert len(erp_invoices(live_sandbox, vendor_id="V001", invoice_number="AI/2026/0917")) == 1
    state = ledger.load_state(ctx2.conn, task_id)
    assert state.notes_of("resume")
    steps = [r.step for r in state.history]
    assert steps == sorted(steps) and len(steps) == len(set(steps))
    blocked = [e for r in state.history for e in r.observation.policy_events if e.type == "duplicate_prevented"]
    assert blocked and "Already done in this task (ERP-INV-000104)" in blocked[0].message
    assert "Processed" in mail_message(live_sandbox, 2)["labels"]


def test_ask_human_then_answer_resumes_with_answer_in_context(live_sandbox, fresh_sandbox):
    script = [
        flows.step("ask_human", "Two vendors are called Acme; the choice changes which invoice is posted.",
                   question="Which Acme did you mean?",
                   options=["Acme Industries Pvt Ltd — AI/2026/0917, INR 42,380.00", "Acme Logistics LLP — ALL-7781, INR 18,900.00"],
                   context="Both have unprocessed invoices in the AP mailbox."),
        lambda messages: (_ for _ in ()).throw(AssertionError("answer missing")) if "Acme Industries, the components supplier" not in messages[-1].content else finish_step("escalated", "Test stops after confirming the answer arrived."),
    ]
    ctx = make_ctx(ScriptedLLM(script, forced={"submit_goal_spec": flows.HAPPY_GOAL}), worker_id="w1")
    task_id = queue.enqueue(ctx.conn, "Enter the pending Acme invoice into the ERP.", "Arjun Rao")
    run_worker(ctx, once=True)
    assert queue.get(ctx.conn, task_id)["status"] == "waiting_human"
    question = inbox.pending_questions(ctx.conn)[0]
    assert json.loads(question["options_json"])[1].startswith("Acme Logistics")
    with pytest.raises(inbox.InboxError, match="not a known person"):
        inbox.answer_question(ctx.conn, ctx.company, question["id"], "x", by="Mallory")
    inbox.answer_question(ctx.conn, ctx.company, question["id"], "Acme Industries, the components supplier.", by="Arjun Rao", remember=False)
    assert queue.get(ctx.conn, task_id)["status"] == "queued"
    run_worker(ctx, once=True)
    assert queue.get(ctx.conn, task_id)["status"] == "escalated"
    state = ledger.load_state(ctx.conn, task_id)
    assert state.answers[0]["answer"].startswith("Acme Industries")


def test_ctrl_c_finishes_the_step_and_returns_task_to_queue(live_sandbox, fresh_sandbox):
    base = fresh_sandbox
    script = flows.happy_path_steps(live_sandbox, base)
    ctx = make_ctx(None, worker_id="w1")

    def press_ctrl_c(messages):
        ctx.stop.set()  # as the SIGINT handler does
        return flows.step("api_get", "Check the vendor.", connector_id="erp", endpoint="search_vendors", params={"q": "Acme"})

    ctx.llm = ScriptedLLM(script[:3] + [press_ctrl_c], forced=flows.happy_llm(live_sandbox, base).forced)
    task_id = queue.enqueue(ctx.conn, REQUEST, "Arjun Rao")
    assert run_worker(ctx, once=True) == 1
    task = queue.get(ctx.conn, task_id)
    assert task["status"] == "queued" and task["lease_owner"] is None
    state = ledger.load_state(ctx.conn, task_id)
    assert [r.tool for r in state.history][-1] == "api_get"  # the in-flight step was completed and checkpointed

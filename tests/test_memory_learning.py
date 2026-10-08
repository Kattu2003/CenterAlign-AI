"""Phase 7: company memory that learns from feedback (scripted agent, real sandbox)."""

from __future__ import annotations

import json

import pytest

from karya.llm.scripted import ScriptedLLM
from karya.memory import store
from karya.memory.learning import learner_for
from karya.runtime import ledger
from karya.worker import inbox, queue
from karya.worker.worker import run_worker
from sandbox.seed.seed import reset_all
from tests import scripted_flows as flows
from tests.test_e2e_scripted import erp_invoices, make_ctx

pytestmark = pytest.mark.browser
REQUEST = "Enter the pending Acme invoice into the ERP."
ANSWER = "When we say Acme we mean Acme Industries, our components supplier. Acme Logistics is always named in full."
FACT = {
    "durable": True,
    "statement": "In accounts-payable requests, 'Acme' means Acme Industries Pvt Ltd (vendor V001), the components supplier; Acme Logistics LLP is the freight vendor and is always named in full.",
    "entities": ["Acme Industries Pvt Ltd", "V001", "Acme Logistics LLP"],
}


def investigate_and_ask():
    return [
        flows.step("api_get", "Find unprocessed invoices from vendors named Acme.", connector_id="mail", endpoint="search_messages", params={"q": "acme", "unlabelled": "1"}),
        flows.step(
            "ask_human",
            "Two different vendors are called Acme; posting the wrong one enters the wrong bill.",
            question="There are pending invoices from two vendors named Acme. Which one did you mean?",
            options=["Acme Industries Pvt Ltd — AI/2026/0917, INR 42,380.00", "Acme Logistics LLP — ALL-7781, INR 18,900.00"],
            context="Found in the AP mailbox, both unlabelled.",
        ),
    ]


def intake_text(llm: ScriptedLLM) -> str:
    first = next(r for r in llm.requests if r["tool_choice"] == {"name": "submit_goal_spec"})
    return "\n".join(m.content for m in first["messages"])


def test_ask_learn_then_no_question_then_forget(live_sandbox, fresh_sandbox):
    base = fresh_sandbox
    fast = ScriptedLLM(forced={"submit_fact": FACT})

    # Run 1: investigate, ask with options, learn from the answer, post the right invoice.
    llm1 = flows.happy_llm(live_sandbox, base)
    llm1.steps.extendleft(reversed(investigate_and_ask()))
    ctx1 = make_ctx(llm1, worker_id="w1")
    t1 = queue.enqueue(ctx1.conn, REQUEST, "Arjun Rao")
    run_worker(ctx1, once=True)
    assert queue.get(ctx1.conn, t1)["status"] == "waiting_human"
    question = inbox.pending_questions(ctx1.conn)[0]
    assert "two vendors named Acme" in question["question"]
    _, fact = inbox.answer_question(ctx1.conn, ctx1.company, question["id"], ANSWER, by="Arjun Rao", learn=learner_for(fast))
    assert fact is not None and fact.id == "F-0001"
    assert fact.source_kind == "human_answer" and fact.created_by == "Arjun Rao" and fact.source_task == t1 and fact.source_ref == question["id"]
    assert "learned from Arjun Rao" in fact.provenance()
    run_worker(ctx1, once=True)
    assert queue.get(ctx1.conn, t1)["status"] == "succeeded"
    assert len(erp_invoices(live_sandbox, invoice_number="AI/2026/0917")) == 1
    assert erp_invoices(live_sandbox, invoice_number="ALL-7781") == []

    # Run 2: reset the company's systems but keep memory -> the fact is in context, no question.
    base = reset_all()
    llm2 = flows.happy_llm(live_sandbox, base)
    ctx2 = make_ctx(llm2, worker_id="w2")
    t2 = queue.enqueue(ctx2.conn, REQUEST, "Arjun Rao")
    run_worker(ctx2, once=True)
    assert queue.get(ctx2.conn, t2)["status"] == "succeeded"
    assert ctx2.conn.execute("SELECT COUNT(*) FROM questions WHERE task_id = ?", (t2,)).fetchone()[0] == 0
    assert "[F-0001]" in intake_text(llm2) and "learned from Arjun Rao" in intake_text(llm2)
    report = json.loads((ctx2.settings.runs_dir / t2 / "report.json").read_text())
    assert [k["id"] for k in report["knowledge_used"]] == ["F-0001"]
    assert "F-0001" in (ctx2.settings.runs_dir / t2 / "report.md").read_text()

    # Forget: the fact no longer reaches the model (a real model would ask again).
    assert store.forget_fact(ctx2.conn, "F-0001", by="Arjun Rao")
    llm3 = ScriptedLLM([flows.step("finish", "Stop here for the test.", status="escalated", summary="test")], forced={"submit_goal_spec": flows.HAPPY_GOAL})
    ctx3 = make_ctx(llm3, worker_id="w3")
    queue.enqueue(ctx3.conn, REQUEST, "Arjun Rao")
    run_worker(ctx3, once=True)
    assert "F-0001" not in intake_text(llm3)
    audit = [r["event"] for r in ctx3.conn.execute("SELECT event FROM audit WHERE event LIKE 'fact_%'")]
    assert audit == ["fact_learned", "fact_forgotten"]


def test_not_durable_answers_are_not_remembered(live_sandbox, fresh_sandbox):
    fast = ScriptedLLM(forced={"submit_fact": {"durable": False}})
    llm = ScriptedLLM(investigate_and_ask(), forced={"submit_goal_spec": flows.HAPPY_GOAL})
    ctx = make_ctx(llm, worker_id="w1")
    queue.enqueue(ctx.conn, REQUEST, "Arjun Rao")
    run_worker(ctx, once=True)
    question = inbox.pending_questions(ctx.conn)[-1]
    before = len(store.list_facts(ctx.conn, include_inactive=True))
    _, fact = inbox.answer_question(ctx.conn, ctx.company, question["id"], "Just this once, the logistics one.", by="Arjun Rao", learn=learner_for(fast))
    assert fact is None and len(store.list_facts(ctx.conn, include_inactive=True)) == before
    # --no-remember skips learning entirely
    assert ctx.conn.execute("SELECT remember FROM questions WHERE id = ?", (question["id"],)).fetchone()["remember"] == 1

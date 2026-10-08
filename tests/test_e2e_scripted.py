"""End-to-end with a real Chromium, the real gateway and the sandbox apps,
driven by ScriptedLLM (no real model). Plan §12 integration tests."""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
import pytest

from karya import db
from karya.company import load_company
from karya.config import get_settings, reset_settings
from karya.runtime import ledger
from karya.runtime.loop import RunContext
from karya.worker import queue
from karya.worker.worker import process_task
from sandbox.common import settings as sandbox_settings
from tests import scripted_flows as flows

pytestmark = pytest.mark.browser

REQUEST = "Find the latest invoice from Acme Industries in the AP mailbox, enter it into the ERP, and tell me when it's done."


def make_ctx(llm, **kwargs) -> RunContext:
    from karya.llm.scripted import ScriptedVerifier

    reset_settings()
    settings = get_settings()
    kwargs.setdefault("verifier_llm", ScriptedVerifier(fail_on_mismatch=["c2"]))
    return RunContext(settings=settings, company=load_company(settings.company_dir), llm=llm, conn=db.connect(settings.db_path), **kwargs)


def run_until_rest(ctx: RunContext, request: str = REQUEST) -> tuple[str, object]:
    task_id = queue.enqueue(ctx.conn, request, "Arjun Rao")
    assert queue.claim(ctx.conn, ctx.worker_id, task_id=task_id) is not None
    return task_id, process_task(task_id, ctx)


def erp_invoices(urls, **params) -> list[dict]:
    return httpx.get(f"{urls['erp']}/api/invoices", params=params, headers={"X-API-Key": sandbox_settings.api_key("erp")}).json()


def mail_message(urls, message_id: int) -> dict:
    return httpx.get(f"{urls['mail']}/api/messages/{message_id}", headers={"X-API-Key": sandbox_settings.api_key("mail")}).json()


def test_happy_path_end_to_end(live_sandbox, fresh_sandbox):
    base = fresh_sandbox
    ctx = make_ctx(flows.happy_llm(live_sandbox, base))
    task_id, result = run_until_rest(ctx)
    assert result.outcome == "done", result
    assert result.status == "succeeded"

    records = erp_invoices(live_sandbox, vendor_id="V001", invoice_number="AI/2026/0917")
    assert len(records) == 1
    record = records[0]
    assert record["amount"] == "42380.00"
    assert record["invoice_date"] == (base - timedelta(days=3)).isoformat()
    assert record["due_date"] == (base + timedelta(days=27)).isoformat()
    assert record["entered_by"] == "karya.agent"
    assert record["erp_ref"] == "ERP-INV-000104"
    assert "Processed" in mail_message(live_sandbox, 2)["labels"]

    state = ledger.load_state(ctx.conn, task_id)
    assert state.goal is not None and len(state.goal.success_criteria) == 3
    assert state.plan
    assert all(r.why for r in state.history), "every tool call carries a why"
    run_dir = ctx.settings.runs_dir / task_id
    assert (run_dir / "trace.jsonl").exists()
    assert list((run_dir / "screens").glob("*.png"))
    assert (run_dir / "downloads" / "AI-2026-0917.pdf").exists()
    assert queue.get(ctx.conn, task_id)["status"] == "succeeded"

    # The extraction was normalized by code and its checks passed.
    extracted = state.facts["extracted:downloads/AI-2026-0917.pdf"]
    assert extracted["values"]["total"] == "42380.00" and extracted["checks_passed"] is True
    # The write went through the intent log and was committed with the ERP reference.
    intents = ctx.conn.execute("SELECT * FROM intents WHERE task_id = ?", (task_id,)).fetchall()
    invoice_intents = [i for i in intents if i["action"] == "create_invoice"]
    assert len(invoice_intents) == 1 and invoice_intents[0]["status"] == "committed"
    assert invoice_intents[0]["record_ref"] == "ERP-INV-000104"
    payload = json.loads(invoice_intents[0]["payload_json"])
    assert "csrf_token" not in payload  # volatile fields are dropped

    # Phase 4: independent verification + evidence report.
    verification = state.verifications[-1]
    assert verification["result"]["overall"] == "pass"
    evidence = " ".join(verification["system_evidence"])
    assert "1 matching record(s) in Meridian ERP (ref ERP-INV-000104)" in evidence
    assert "amount matches" in evidence and "due_date matches" in evidence
    report = (run_dir / "report.md").read_text()
    assert "Succeeded" in report and "| c1 " in report and "| c2 " in report and "| c3 " in report
    assert report.count("| pass |") == 3
    assert "erp:ERP-INV-000104" in report and "downloads/AI-2026-0917.pdf" in report
    data = json.loads((run_dir / "report.json").read_text())
    assert data["stats"]["writes_committed"] == 2 and data["stats"]["verifier_rounds"] == 1
    assert any(a["tool"] == "files_extract" for a in data["what_i_did"])
    assert ctx.conn.execute("SELECT outcome FROM episodes WHERE task_id = ?", (task_id,)).fetchone()["outcome"] == "succeeded"


def test_vertex_invoice_is_held_for_approval(live_sandbox, fresh_sandbox):
    from karya.gateway import approvals

    ctx = make_ctx(flows.vertex_llm(live_sandbox, fresh_sandbox))
    task_id, result = run_until_rest(ctx, "Process the Vertex Semiconductors invoice in the AP mailbox.")
    assert result.outcome == "suspended" and result.status == "waiting_approval"
    assert queue.get(ctx.conn, task_id)["status"] == "waiting_approval"
    assert queue.get(ctx.conn, task_id)["lease_owner"] is None  # lease released while waiting
    pending = [a for a in approvals.for_task(ctx.conn, task_id) if a["status"] == "pending"]
    assert len(pending) == 1
    apr = pending[0]
    assert apr["approver_role"] == "finance_manager" and json.loads(apr["rule_ids_json"]) == ["AP-001"]
    assert "VS-55120" in apr["summary"] and "184500.00" in apr["summary"]
    assert erp_invoices(live_sandbox, invoice_number="VS-55120") == []  # nothing was posted
    state = ledger.load_state(ctx.conn, task_id)
    last = state.history[-1].observation
    assert last.policy_events[0].type == "approval_required"
    assert "Held for approval" in last.content  # the browser shows the gateway's 202 page


def test_verifier_catches_a_corrupted_record(live_sandbox, fresh_sandbox):
    """Phase 4 acceptance: change the ERP amount between finish and verify -> c2 fails, the
    loop gets feedback, and the task cannot end as succeeded."""
    from sandbox.common.db import opened

    def corrupt(task_id: str) -> None:
        with opened("erp") as conn:
            conn.execute("UPDATE invoices SET amount_paise = 4328000 WHERE invoice_number = 'AI/2026/0917'")

    llm = flows.happy_llm(live_sandbox, fresh_sandbox)
    llm.steps.append(flows.step("finish", "Retry after feedback.", status="completed", summary="Posted AI/2026/0917.", claims=[]))
    ctx = make_ctx(llm, hooks={"before_verify": corrupt})
    task_id, result = run_until_rest(ctx)
    state = ledger.load_state(ctx.conn, task_id)
    first = state.verifications[0]["result"]
    assert first["overall"] == "fail"
    verdicts = {v["criterion_id"]: v["verdict"] for v in first["verdicts"]}
    assert verdicts["c2"] == "fail" and verdicts["c1"] == "pass"
    assert "amount submitted 42380.00" in first["notes"] and "43280.00" in first["notes"]
    feedback = state.notes_of("verifier_feedback")
    assert feedback and "c2 fail" in feedback[0]["text"]
    assert result.status == "partial"  # two failed verification rounds, some criteria pass
    assert len(state.verifications) == 2

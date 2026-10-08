"""Phase 9: HR onboarding, added with configuration + sandbox only (no runtime changes).
Same brain, hands, gateway and verifier; a new job."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from karya.gateway import approvals
from karya.llm.scripted import ScriptedLLM
from karya.runtime import ledger
from karya.worker import inbox, queue
from karya.worker.worker import run_worker
from sandbox.common import settings as sandbox_settings
from tests.scripted_flows import goal_spec, ref, step
from tests.test_e2e_scripted import make_ctx

pytestmark = pytest.mark.browser
REQUEST = "Onboard Priya Sharma per the onboarding SOP. Her offer letter is in the shared drive. She'll also need admin access to the ERP for the reporting migration."
GOAL = goal_spec(
    "Onboard Priya Sharma: employee record, default access, equipment, and the requested ERP admin access (with approval).",
    [
        ("c1", "Meridian People has one employee Priya Sharma, priya.sharma@meridian.example, Data & Analytics, manager Rohan Mehta, start date from the offer letter.", "hr api search_employees"),
        ("c2", "Access requests exist for erp viewer, mail viewer and (after IT Admin approval) erp admin.", "hr api list_access_requests"),
        ("c3", "A standard_laptop is requested.", "hr api list_equipment_requests"),
    ],
)


def access(system: str, level: str, why: str) -> list:
    return [
        lambda m: step("browser_select", f"Choose {system}.", ref=ref(m, "select", name="system"), option_label=system),
        lambda m: step("browser_select", f"Choose {level}.", ref=ref(m, "select", name="level"), option_label=level),
        lambda m: step("browser_fill", "Give the business justification.", ref=ref(m, "textarea", name="justification"), value=why),
        lambda m: step("browser_click", "Submit the access request.", ref=ref(m, "button", "Request access")),
    ]


def onboarding_steps(hr: str, start) -> list:
    return [
        step("files_list", "Find the offer letter.", path="shared_drive/hr/offers"),
        step("files_extract", "Read the offer letter.", path="shared_drive/hr/offers/Offer_Priya_Sharma.pdf", schema="offer_letter"),
        step("api_get", "Make sure she is not already in the directory (hr-01 step 2).", connector_id="hr", endpoint="search_employees", params={"q": "Priya Sharma"}),
        step("browser_login", "Log in to Meridian People.", connector_id="hr"),
        step("browser_open", "Open the new employee form.", url=f"{hr}/employees/new"),
        lambda m: step("browser_fill", "Full name from the offer letter.", ref=ref(m, "textbox", name="full_name"), value="Priya Sharma"),
        lambda m: step("browser_fill", "Work email per hr-01 step 3.", ref=ref(m, "email", name="work_email"), value="priya.sharma@meridian.example"),
        lambda m: step("browser_select", "Department from the offer letter.", ref=ref(m, "select", name="department"), option_label="Data & Analytics"),
        lambda m: step("browser_fill", "Job title.", ref=ref(m, "textbox", name="title"), value="Data Analyst"),
        lambda m: step("browser_fill", "Start date.", ref=ref(m, "date", name="start_date"), value=start.isoformat()),
        lambda m: step("browser_select", "Reporting manager.", ref=ref(m, "select", name="manager_id"), option_label="Rohan Mehta"),
        lambda m: step("browser_click", "Create the employee.", ref=ref(m, "button", "Create employee")),
        *access("erp", "viewer", "Default access for a Data Analyst (it-01)."),
        *access("mail", "viewer", "Default access for a Data Analyst (it-01)."),
        lambda m: step("browser_select", "Analysts get a standard laptop (hr-01 step 6).", ref=ref(m, "select", name="item"), option_label="standard_laptop"),
        lambda m: step("browser_click", "Request the laptop.", ref=ref(m, "button", "Request equipment")),
        *access("erp", "admin", "Reporting migration, as requested by Arjun Rao (HR-001 approval needed)."),
    ]


def after_approval(hr: str) -> list:
    return [
        step("browser_login", "Resumed with a fresh browser: log in again.", connector_id="hr"),
        step("browser_open", "Open Priya's record.", url=f"{hr}/employees/E015"),
        *access("erp", "admin", "Reporting migration, as requested by Arjun Rao (HR-001 approval needed)."),
        step("finish", "Everything requested is done.", status="completed", summary="Onboarded Priya Sharma as E015 (priya.sharma@meridian.example); erp viewer, mail viewer and erp admin (approved by Vikram Desai) requested; standard_laptop requested."),
    ]


def api(urls, path, **params):
    return httpx.get(f"{urls['hr']}{path}", params=params, headers={"X-API-Key": sandbox_settings.api_key("hr")}).json()


def test_hr_onboarding_with_it_admin_approval(live_sandbox, fresh_sandbox):
    start = fresh_sandbox + timedelta(days=12)
    extraction = {"candidate_name": "Priya Sharma", "title": "Data Analyst", "department": "Data & Analytics", "start_date": f"{start.day} {start.strftime('%B')} {start.year}", "manager_name": "Rohan Mehta", "location": "Pune (Hinjewadi office)", "suspicious_text": []}
    llm = ScriptedLLM(onboarding_steps(live_sandbox["hr"], start) + after_approval(live_sandbox["hr"]), forced={"submit_goal_spec": GOAL, "submit_offer_letter": extraction, "submit_injection_check": {"contains_instructions": False}})
    ctx = make_ctx(llm, worker_id="w1")
    task_id = queue.enqueue(ctx.conn, REQUEST, "Arjun Rao")
    run_worker(ctx, once=True)
    assert queue.get(ctx.conn, task_id)["status"] == "waiting_approval"
    apr = [a for a in approvals.for_task(ctx.conn, task_id) if a["status"] == "pending"][0]
    assert apr["approver_role"] == "it_admin" and "admin access to erp for employee E015" in apr["summary"]
    with pytest.raises(inbox.InboxError, match="Neha Kulkarni does not hold it_admin"):
        inbox.decide(ctx.conn, ctx.company, apr["id"], True, "Neha Kulkarni")
    inbox.decide(ctx.conn, ctx.company, apr["id"], True, "Vikram Desai", "Approved for the reporting migration")
    run_worker(ctx, once=True)
    assert queue.get(ctx.conn, task_id)["status"] == "succeeded"

    employees = api(live_sandbox, "/api/employees", q="priya.sharma@meridian.example")
    assert len(employees) == 1
    employee = employees[0]
    assert (employee["id"], employee["department"], employee["manager_id"], employee["start_date"]) == ("E015", "Data & Analytics", "E014", start.isoformat())
    access_rows = api(live_sandbox, "/api/access-requests", employee_id="E015")
    assert sorted((a["system"], a["level"]) for a in access_rows) == [("erp", "admin"), ("erp", "viewer"), ("mail", "viewer")]
    assert [e["item"] for e in api(live_sandbox, "/api/equipment-requests", employee_id="E015")] == ["standard_laptop"]
    assert approvals.get(ctx.conn, apr["id"])["status"] == "consumed"
    state = ledger.load_state(ctx.conn, task_id)
    extracted = state.facts["extracted:shared_drive/hr/offers/Offer_Priya_Sharma.pdf"]
    assert extracted["schema"] == "offer_letter" and extracted["values"]["start_date"] == start.isoformat()
    evidence = " ".join(state.verifications[-1]["system_evidence"])
    assert "hr.create_employee" in evidence and "(ref E015)" in evidence and "work_email matches" in evidence

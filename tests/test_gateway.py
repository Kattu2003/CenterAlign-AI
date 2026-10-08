"""Gateway decision pipeline (plan §12): a pure function over a request description,
the database and an injectable reconcile lookup. No browser, no servers."""

from __future__ import annotations

from urllib.parse import urlencode

import pytest

from karya import db
from karya.company import load_company
from karya.config import get_settings, reset_settings
from karya.gateway import approvals, intents
from karya.gateway.gateway import Gateway, RequestInfo
from karya.gateway.intents import LookupResult
from karya.gateway.secrets import Secrets
from karya.worker import queue
from tests.conftest import REPO_COMPANY

ERP = "http://127.0.0.1:8101"
FORM = "application/x-www-form-urlencoded"


class FakeLookup:
    """Stands in for the ERP read API: a set of existing (vendor, invoice) records."""

    def __init__(self, existing: dict[tuple[str, str], str] | None = None, fail: bool = False) -> None:
        self.existing = existing or {}
        self.fail = fail
        self.calls = 0

    def check(self, connector, action, values) -> LookupResult:
        self.calls += 1
        if self.fail:
            return LookupResult(exists=None, error="lookup down")
        key = (values.get("vendor_id"), str(values.get("invoice_number", "")).casefold())
        for (vendor, number), ref in self.existing.items():
            if (vendor, number.casefold()) == key:
                return LookupResult(exists=True, record_ref=ref, count=1)
        return LookupResult(exists=False)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("KARYA_DB_PATH", str(tmp_path / "gw.db"))
    reset_settings()
    conn = db.connect(get_settings().db_path)
    company = load_company(REPO_COMPANY)
    task_id = queue.enqueue(conn, "test", "Arjun Rao")
    lookup = FakeLookup()
    gateway = Gateway(task_id, company, conn, Secrets(company.connectors), lookup=lookup)
    yield gateway, conn, task_id, lookup
    conn.close()
    reset_settings()


def post(path: str, **fields) -> RequestInfo:
    return RequestInfo("POST", ERP + path, urlencode({"csrf_token": "abc", **fields}).encode(), FORM)


def invoice(amount: str = "42380.00", number: str = "AI/2026/0917", vendor: str = "V001") -> RequestInfo:
    return post("/invoices", vendor_id=vendor, invoice_number=number, invoice_date="2026-10-05", due_date="2026-11-04", amount=amount)


def test_unknown_host_is_aborted(env):
    gateway, conn, *_ = env
    decision = gateway.decide(RequestInfo("GET", "https://evil.example/steal"))
    assert decision.kind == "abort"
    assert gateway.drain_events()[0].type == "blocked_host"
    assert gateway.decide(RequestInfo("GET", "http://localhost:8101/")).kind == "abort"  # not the declared origin
    audit_events = [r["event"] for r in conn.execute("SELECT event FROM audit")]
    assert "blocked_host" in audit_events


def test_blocked_paths(env):
    gateway, *_ = env
    decision = gateway.decide(RequestInfo("GET", ERP + "/__admin/state"))
    assert decision.kind == "fulfill" and decision.status == 403
    assert gateway.decide(RequestInfo("POST", ERP + "/__admin/reset")).status == 403


def test_reads_are_allowed(env):
    gateway, *_ = env
    assert gateway.decide(RequestInfo("GET", ERP + "/invoices?q=acme")).kind == "continue"
    assert gateway.decide(RequestInfo("GET", "data:text/plain,hi")).kind == "continue"


def test_unknown_write_is_denied(env):
    gateway, *_ = env
    decision = gateway.decide(post("/invoices/4/delete"))
    assert decision.kind == "fulfill" and decision.status == 403
    event = gateway.drain_events()[0]
    assert event.type == "denied" and event.rule_ids == ["unknown_write"]


def test_auth_actions_pass_even_read_only(env):
    gateway, *_ = env
    gateway.mode = "read_only"
    assert gateway.decide(post("/login", username="x", password="y")).kind == "continue"


def test_read_only_mode_blocks_writes(env):
    gateway, *_ = env
    gateway.mode = "read_only"
    decision = gateway.decide(invoice())
    assert decision.status == 403
    assert gateway.drain_events()[0].type == "read_only_block"


def test_policy_deny_ap002(env):
    gateway, conn, *_ = env
    decision = gateway.decide(post("/vendors/V004/bank", account_number="50200099887766", ifsc="HDFC0001234", reason="changed"))
    assert decision.kind == "fulfill" and decision.status == 403
    assert "Blocked by Meridian policy AP-002" in decision.body
    event = gateway.drain_events()[0]
    assert event.type == "denied" and event.rule_ids == ["AP-002"]
    row = conn.execute("SELECT * FROM audit WHERE event = 'policy_denied'").fetchone()
    assert row is not None and "AP-002" in row["details_json"]


def test_small_invoice_is_sent(env):
    gateway, *_ = env
    decision = gateway.decide(invoice("42380.00"))
    assert decision.kind == "send"
    assert decision.idem_key == "erp:create_invoice:v001|ai/2026/0917"
    assert "csrf_token" not in decision.payload


def test_approval_hold_then_grant_only_for_identical_scope(env):
    gateway, conn, task_id, _ = env
    held = gateway.decide(invoice("184500.00", "VS-55120", "V003"))
    assert held.kind == "fulfill" and held.status == 202
    event = gateway.drain_events()[0]
    assert event.type == "approval_required" and event.rule_ids == ["AP-001"]
    apr = approvals.get(conn, event.approval_id)
    assert apr["status"] == "pending" and apr["approver_role"] == "finance_manager"
    # Resubmitting while pending reuses the same approval (no duplicate requests).
    gateway.decide(invoice("184500.00", "VS-55120", "V003"))
    assert gateway.drain_events()[0].approval_id == apr["id"]

    approvals.decide(conn, gateway.company, apr["id"], True, "Neha Kulkarni", "ok")
    granted = gateway.decide(invoice("184500.00", "VS-55120", "V003"))
    assert granted.kind == "send" and granted.approval_id == apr["id"]
    assert gateway.drain_events()[0].type == "approved_use"

    # A different amount is a different scope: it needs a new approval.
    other = gateway.decide(invoice("184600.00", "VS-55120", "V003"))
    assert other.status == 202
    new_event = gateway.drain_events()[0]
    assert new_event.approval_id != apr["id"]


def test_consumed_approval_is_not_reusable(env):
    gateway, conn, task_id, _ = env
    gateway.decide(invoice("184500.00", "VS-55120", "V003"))
    apr_id = gateway.drain_events()[0].approval_id
    approvals.decide(conn, gateway.company, apr_id, True, "Neha Kulkarni")
    approvals.consume(conn, apr_id)
    again = gateway.decide(invoice("184500.00", "VS-55120", "V003"))
    assert again.status == 202  # held again: a consumed approval cannot authorise another write
    assert gateway.drain_events()[0].approval_id != apr_id


def test_duplicate_prevented_from_committed_intent(env):
    gateway, conn, task_id, _ = env
    first = gateway.decide(invoice())
    intent_id = intents.create(conn, task_id, "erp", "create_invoice", first.idem_key, first.payload)
    intents.record_response(conn, intent_id, "committed", 303, record_ref="ERP-INV-000104")
    second = gateway.decide(invoice())
    assert second.kind == "fulfill" and second.status == 409
    event = gateway.drain_events()[0]
    assert event.type == "duplicate_prevented" and "ERP-INV-000104" in event.message


def test_pre_write_existence_check_blocks_duplicates(env):
    gateway, conn, task_id, lookup = env
    lookup.existing[("V005", "INV-OE-2291")] = "ERP-INV-000102"
    decision = gateway.decide(invoice("56212.00", "INV-OE-2291", "V005"))
    # Duplicate check runs before the approval hold: nobody is asked to approve a duplicate.
    assert decision.status == 409
    event = gateway.drain_events()[0]
    assert event.type == "duplicate_prevented" and "ERP-INV-000102" in event.message
    assert approvals.pending(conn) == []


def test_precheck_unavailable_fails_closed(env):
    gateway, *_ = env
    gateway.lookup = FakeLookup(fail=True)
    decision = gateway.decide(invoice())
    assert decision.status == 503
    assert gateway.drain_events()[0].rule_ids == ["precheck_unavailable"]


def test_unknown_intent_is_reconciled_before_retry(env):
    gateway, conn, task_id, lookup = env
    first = gateway.decide(invoice())
    intent_id = intents.create(conn, task_id, "erp", "create_invoice", first.idem_key, first.payload)
    intents.record_response(conn, intent_id, "unknown", 504)
    lookup.existing[("V001", "AI/2026/0917")] = "ERP-INV-000104"  # it did commit
    retry = gateway.decide(invoice())
    assert retry.status == 409
    assert intents.get(conn, intent_id)["status"] == "committed"
    assert intents.get(conn, intent_id)["reconciled"] == 1


def test_record_outcome_classification(env):
    gateway, conn, task_id, lookup = env
    company = gateway.company
    erp = company.connectors["erp"]
    action = erp.action("create_invoice")
    payload = {"vendor_id": "V001", "invoice_number": "X-1"}
    # 303 to the login page = session expired: failed, nothing submitted.
    i1 = intents.create(conn, task_id, "erp", "create_invoice", "k1", payload)
    assert gateway.record_outcome(i1, erp, action, payload, 303, "/login?next=/invoices/new") == "failed"
    # 409 lock = failed (safe to retry).
    i2 = intents.create(conn, task_id, "erp", "create_invoice", "k2", payload)
    assert gateway.record_outcome(i2, erp, action, payload, 409) == "failed"
    # 504 + record exists = committed via reconciliation.
    lookup.existing[("V001", "X-1")] = "ERP-INV-000150"
    i3 = intents.create(conn, task_id, "erp", "create_invoice", "k3", payload)
    assert gateway.record_outcome(i3, erp, action, payload, 504) == "committed"
    events = gateway.drain_events()
    assert any("the record exists (ERP-INV-000150)" in e.message for e in events)
    # 504 + no record = failed via reconciliation ("safe to submit again").
    i4 = intents.create(conn, task_id, "erp", "create_invoice", "k4", {"vendor_id": "V001", "invoice_number": "X-2"})
    assert gateway.record_outcome(i4, erp, action, {"vendor_id": "V001", "invoice_number": "X-2"}, 504) == "failed"
    assert "safe to submit again" in gateway.drain_events()[-1].message


def test_file_write_checks(env):
    gateway, *_ = env
    assert gateway.check_file_write("workspace", "notes.md")[0]
    assert gateway.check_file_write("shared_drive", "outbox/report.csv")[0]
    assert not gateway.check_file_write("shared_drive", "finance/vendor_list.csv")[0]
    gateway.mode = "read_only"
    assert not gateway.check_file_write("workspace", "notes.md")[0]


def test_approval_rbac(env):
    gateway, conn, task_id, _ = env
    gateway.decide(invoice("184500.00", "VS-55120", "V003"))
    apr_id = gateway.drain_events()[0].approval_id
    with pytest.raises(approvals.ApprovalError, match="Arjun Rao does not hold finance_manager"):
        approvals.decide(conn, gateway.company, apr_id, True, "Arjun Rao")
    with pytest.raises(approvals.ApprovalError, match="not a known person"):
        approvals.decide(conn, gateway.company, apr_id, True, "Mallory")
    with pytest.raises(approvals.ApprovalError, match="needs a reason"):
        approvals.decide(conn, gateway.company, apr_id, False, "Neha Kulkarni", "")
    decided = approvals.decide(conn, gateway.company, apr_id, False, "Neha Kulkarni", "PO not received")
    assert decided["status"] == "rejected"
    blocked = gateway.decide(invoice("184500.00", "VS-55120", "V003"))
    assert blocked.status == 403 and "PO not received" in gateway.drain_events()[0].message

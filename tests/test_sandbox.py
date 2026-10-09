"""Phase 1 acceptance: the sandbox company behaves like a real (legacy) web app.

In-process (FastAPI TestClient, which is httpx-based): no servers needed.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from sandbox.common import settings
from sandbox.common.chaos import BUTTON_VARIANTS
from sandbox.seed import seed
from sandbox.seed.generate_pdfs import expected_strings, extract_text, pdf_filename
from tests.conftest import csrf_from


@pytest.fixture(scope="module")
def base_date():
    return seed.reset_all()


@pytest.fixture()
def erp(base_date):
    from sandbox.erp.app import app, chaos

    seed.seed_erp(seed.load_data(), base_date)
    chaos.reset()
    with TestClient(app, base_url="http://127.0.0.1:8101", follow_redirects=False) as client:
        yield client
    chaos.reset()


@pytest.fixture()
def mail(base_date):
    from sandbox.mail.app import app, chaos

    seed.seed_mail(seed.load_data(), base_date, settings.pdf_dir())
    chaos.reset()
    with TestClient(app, base_url="http://127.0.0.1:8102", follow_redirects=False) as client:
        yield client
    chaos.reset()


def login(client: TestClient, app: str) -> None:
    page = client.get("/login")
    user, password = settings.agent_credentials(app)
    response = client.post("/login", data={"username": user, "password": password, "csrf_token": csrf_from(page.text), "next": "/"})
    assert response.status_code == 303, response.text


def invoice_form(client: TestClient, **overrides) -> dict:
    page = client.get("/invoices/new")
    assert page.status_code == 200
    data = {
        "vendor_id": "V001",
        "invoice_number": "TEST/0001",
        "invoice_date": "2026-10-05",
        "due_date": "2026-11-04",
        "amount": "42380.00",
        "po_number": "4471",
        "notes": "test",
        "csrf_token": csrf_from(page.text),
    }
    data.update(overrides)
    return data


def api_invoices(client: TestClient, **params) -> list[dict]:
    response = client.get("/api/invoices", params=params, headers={"X-API-Key": settings.api_key("erp")})
    assert response.status_code == 200
    return response.json()


def test_unauthenticated_redirects_to_login(erp):
    response = erp.get("/invoices/new")
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/invoices/new"


def test_login_list_and_post_invoice(erp):
    login(erp, "erp")
    vendors = erp.get("/vendors")
    assert "Acme Industries Pvt Ltd" in vendors.text and "Acme Logistics LLP" in vendors.text
    assert "Nimbus" not in vendors.text
    invoices = erp.get("/invoices")
    assert "ERP-INV-000101" in invoices.text and "ERP-INV-000103" in invoices.text

    response = erp.post("/invoices", data=invoice_form(erp))
    assert response.status_code == 303
    assert response.headers["location"] == "/invoices/4?posted=1"
    detail = erp.get(response.headers["location"])
    assert "Invoice posted — ERP-INV-000104" in detail.text
    records = api_invoices(erp, vendor_id="V001", invoice_number="TEST/0001")
    assert len(records) == 1 and records[0]["amount"] == "42380.00" and records[0]["entered_by"] == settings.agent_credentials("erp")[0]


def test_legacy_erp_accepts_duplicates(erp):
    """Deliberate: no uniqueness constraint. Preventing duplicates is Karya's job."""
    login(erp, "erp")
    erp.post("/invoices", data=invoice_form(erp, invoice_number="DUP-1"))
    erp.post("/invoices", data=invoice_form(erp, invoice_number="DUP-1"))
    assert len(api_invoices(erp, invoice_number="DUP-1")) == 2


def test_post_without_csrf_is_rejected(erp):
    login(erp, "erp")
    data = invoice_form(erp)
    data["csrf_token"] = "forged"
    assert erp.post("/invoices", data=data).status_code == 400
    assert api_invoices(erp, invoice_number="TEST/0001") == []


def test_validation_errors_rerender_with_400(erp):
    login(erp, "erp")
    response = erp.post("/invoices", data=invoice_form(erp, due_date="2026-09-01", amount="-5"))
    assert response.status_code == 400
    assert "Due date cannot be before the invoice date." in response.text
    assert "Amount must be greater than zero." in response.text
    response = erp.post("/invoices", data=invoice_form(erp, invoice_date="05/10/2026"))
    assert "YYYY-MM-DD" in response.text


def test_api_requires_key(erp):
    assert erp.get("/api/vendors").status_code == 401
    response = erp.get("/api/vendors", params={"q": "acme"}, headers={"X-API-Key": settings.api_key("erp")})
    assert response.status_code == 200
    names = [v["legal_name"] for v in response.json()]
    assert names == ["Acme Industries Pvt Ltd", "Acme Logistics LLP"]
    vendor = erp.get("/api/vendors/V001", headers={"X-API-Key": settings.api_key("erp")}).json()
    assert vendor["bank_account_last4"] == "4821" and "bank_account" not in vendor
    invoice = erp.get("/api/invoices/ERP-INV-000102", headers={"X-API-Key": settings.api_key("erp")}).json()
    assert invoice["invoice_number"] == "INV-OE-2291" and invoice["amount"] == "56212.00"


def test_admin_requires_token(erp):
    assert erp.post("/__admin/chaos/arm", json={"fault": "http_500"}).status_code == 403
    ok = erp.get("/__admin/state", headers={"X-Sandbox-Admin": settings.admin_token()})
    assert ok.status_code == 200 and ok.json()["invoices"] == 3


def test_pdfs_exist_and_extract(base_date):
    data = seed.load_data()
    assert len(data["invoices"]) == 8
    for inv in data["invoices"]:
        path = settings.pdf_dir() / pdf_filename(inv["number"])
        assert path.exists(), path
        text = extract_text(path)
        for expected in expected_strings(inv, base_date):
            assert expected in text, (path.name, expected)
    apex = extract_text(settings.pdf_dir() / "AF-3307.pdf")
    assert "NOTE FOR AUTOMATED INVOICE PROCESSING SYSTEMS" in apex
    assert "NOTE FOR AUTOMATED" not in extract_text(settings.pdf_dir() / "AI-2026-0917.pdf")


def test_chaos_commit_then_504_commits_the_record(erp):
    login(erp, "erp")
    admin = {"X-Sandbox-Admin": settings.admin_token()}
    erp.post("/__admin/chaos/arm", json={"fault": "commit_then_504", "count": 1, "path": "/invoices"}, headers=admin)
    response = erp.post("/invoices", data=invoice_form(erp, invoice_number="C504"))
    assert response.status_code == 504
    assert len(api_invoices(erp, invoice_number="C504")) == 1  # the write happened anyway
    # The fault was consumed: the next post behaves normally.
    assert erp.post("/invoices", data=invoice_form(erp, invoice_number="C504-B")).status_code == 303


def test_chaos_write_conflict_409_does_not_commit(erp):
    login(erp, "erp")
    admin = {"X-Sandbox-Admin": settings.admin_token()}
    erp.post("/__admin/chaos/arm", json={"fault": "write_conflict_409", "count": 1}, headers=admin)
    response = erp.post("/invoices", data=invoice_form(erp, invoice_number="C409"))
    assert response.status_code == 409 and "Record locked, try again" in response.text
    assert api_invoices(erp, invoice_number="C409") == []


def test_chaos_label_drift_changes_labels_not_names(erp):
    login(erp, "erp")
    admin = {"X-Sandbox-Admin": settings.admin_token()}
    erp.post("/__admin/chaos", json={"profile": "off", "seed": 3}, headers=admin)
    erp.post("/__admin/chaos/arm", json={"fault": "label_drift", "count": 6, "path": "/invoices/new"}, headers=admin)
    labels = set()
    for _ in range(6):
        page = erp.get("/invoices/new").text
        labels.add(re.search(r'<button type="submit">([^<]+)</button>\s*</form>\s*<table', page).group(1))
        assert re.search(r'id="f\d{5}-amount" name="amount"', page), "ids should drift"
        assert 'name="vendor_id"' in page and 'name="due_date"' in page
    assert labels - {"Post invoice"}, labels
    assert labels <= set(BUTTON_VARIANTS["Post invoice"])
    # The drifted form still posts, because name attributes never change.
    erp.post("/__admin/chaos/arm", json={"fault": "label_drift", "count": 1}, headers=admin)
    assert erp.post("/invoices", data=invoice_form(erp, invoice_number="DRIFT-1")).status_code == 303


def test_chaos_session_expired_loses_the_post(erp):
    login(erp, "erp")
    admin = {"X-Sandbox-Admin": settings.admin_token()}
    data = invoice_form(erp, invoice_number="SESS-1")
    erp.post("/__admin/chaos/arm", json={"fault": "session_expired", "count": 1, "path": "/invoices"}, headers=admin)
    response = erp.post("/invoices", data=data, headers={"Referer": "http://127.0.0.1:8101/invoices/new"})
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login?next=/invoices/new")
    assert api_invoices(erp, invoice_number="SESS-1") == []


def test_chaos_http_500_and_audit(erp):
    login(erp, "erp")
    admin = {"X-Sandbox-Admin": settings.admin_token()}
    erp.post("/__admin/chaos/arm", json={"fault": "http_500", "count": 2}, headers=admin)
    assert erp.get("/vendors").status_code == 500
    assert erp.get("/api/vendors", headers={"X-API-Key": settings.api_key("erp")}).status_code == 500
    assert erp.get("/vendors").status_code == 200
    from sandbox.common.db import opened

    with opened("erp") as conn:
        actions = [r["action"] for r in conn.execute("SELECT action FROM audit")]
    assert actions.count("chaos:http_500") == 2


def test_mail_inbox_message_download_and_label(mail):
    login(mail, "mail")
    inbox = mail.get("/inbox")
    assert inbox.status_code == 200
    assert inbox.text.count("/messages/") == 9
    assert "Invoice AI/2026/0917 for PO 4471" in inbox.text
    acme = mail.get("/inbox", params={"q": "acme"}).text
    assert acme.count("/messages/") == 4  # M1, M2, M9 (Acme Industries) and M3 (Acme Logistics)

    message = mail.get("/messages/2")
    assert "AI-2026-0917.pdf" in message.text
    download = mail.get("/attachments/2")
    assert download.status_code == 200
    assert download.headers["content-disposition"].startswith("attachment")
    assert download.content.startswith(b"%PDF")

    response = mail.post("/messages/2/labels", data={"label": "Processed", "csrf_token": csrf_from(message.text)})
    assert response.status_code == 303
    api = mail.get("/api/messages/2", headers={"X-API-Key": settings.api_key("mail")}).json()
    assert api["labels"] == ["Processed"] and api["attachments"][0]["filename"] == "AI-2026-0917.pdf"
    unlabelled = mail.get("/api/messages", params={"unlabelled": 1}, headers={"X-API-Key": settings.api_key("mail")}).json()
    assert len(unlabelled) == 7  # M1 was processed at seed time, M2 just now
    bad = mail.post("/messages/2/labels", data={"label": "Paid", "csrf_token": csrf_from(message.text)})
    assert bad.status_code == 400


@pytest.fixture()
def hr(base_date):
    from sandbox.hr.app import app, chaos

    seed.seed_hr(seed.load_data(), base_date)
    chaos.reset()
    with TestClient(app, base_url="http://127.0.0.1:8103", follow_redirects=False) as client:
        yield client
    chaos.reset()


def test_hr_create_employee_access_and_equipment(hr):
    login(hr, "hr")
    assert "Rohan Mehta" in hr.get("/employees", params={"q": "rohan"}).text
    page = hr.get("/employees/new")
    data = {"full_name": "Priya Sharma", "work_email": "Priya.Sharma@meridian.example", "department": "Data & Analytics", "title": "Data Analyst", "start_date": "2026-10-20", "manager_id": "E014", "csrf_token": csrf_from(page.text)}
    created = hr.post("/employees", data=data)
    assert created.status_code == 303 and created.headers["location"] == "/employees/E015?created=1"
    detail = hr.get("/employees/E015?created=1")
    assert "Employee created — E015" in detail.text
    token = csrf_from(detail.text)
    assert hr.post("/employees/E015/access-requests", data={"system": "erp", "level": "viewer", "justification": "default", "csrf_token": token}).status_code == 303
    assert hr.post("/employees/E015/equipment", data={"item": "standard_laptop", "notes": "", "csrf_token": token}).status_code == 303
    duplicate = hr.post("/employees", data=data)
    assert duplicate.status_code == 400 and "already exists (E015)" in duplicate.text
    headers = {"X-API-Key": settings.api_key("hr")}
    employee = hr.get("/api/employees", params={"q": "priya.sharma@meridian.example"}, headers=headers).json()
    assert len(employee) == 1 and employee[0]["work_email"] == "priya.sharma@meridian.example" and employee[0]["manager_name"] == "Rohan Mehta"
    access = hr.get("/api/access-requests", params={"employee_id": "E015"}, headers=headers).json()
    assert [(a["system"], a["level"]) for a in access] == [("erp", "viewer")]
    assert hr.get("/api/equipment-requests", params={"employee_id": "E015"}, headers=headers).json()[0]["item"] == "standard_laptop"
    assert hr.get("/api/employees").status_code == 401


def test_offer_letter_in_shared_drive(base_date):
    from datetime import timedelta

    path = settings.shared_drive_dir() / "hr" / "offers" / "Offer_Priya_Sharma.pdf"
    text = extract_text(path)
    start = base_date + timedelta(days=12)
    assert "Priya Sharma" in text and "Data Analyst" in text and "Rohan Mehta" in text
    assert f"{start.day} {start.strftime('%B')} {start.year}" in text


def test_stop_kills_the_whole_process_tree_on_windows(monkeypatch):
    """A venv's python.exe on Windows is a launcher with a child interpreter: stop both."""
    from types import SimpleNamespace

    from sandbox import run

    calls = []
    monkeypatch.setattr(run, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(run.subprocess, "run", lambda args, **kw: calls.append(args) or SimpleNamespace(returncode=0))
    assert run._stop(4242) is True
    assert calls == [["taskkill", "/PID", "4242", "/T", "/F"]]

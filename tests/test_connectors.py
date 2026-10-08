"""Connector loader and company config tests (plan §12)."""

from __future__ import annotations

from tests.conftest import REPO_COMPANY
from karya.company import load_company, parse_handbook
from karya.connectors.models import compile_template, normalize_origin


def test_path_template_matching():
    pattern = compile_template("/vendors/{vendor_id}/bank")
    assert pattern.match("/vendors/V004/bank").groupdict() == {"vendor_id": "V004"}
    assert pattern.match("/vendors/V004/bank/extra") is None
    assert pattern.match("/vendors//bank") is None
    assert compile_template("/invoices").match("/invoices.json") is None  # dots are literal


def test_match_declared_action_and_unknown_writes():
    company = load_company(REPO_COMPANY)
    erp = company.connectors["erp"]
    action, params = erp.match_action("POST", "/vendors/V004/bank")
    assert action.name == "update_vendor_bank" and params == {"vendor_id": "V004"}
    assert erp.match_action("post", "/invoices")[0].name == "create_invoice"
    assert erp.match_action("POST", "/invoices/4/delete") is None  # undeclared write
    assert erp.match_action("DELETE", "/invoices") is None
    mail = company.connectors["mail"]
    action, params = mail.match_action("POST", "/messages/2/labels")
    assert action.name == "label_message" and params == {"message_id": "2"}


def test_blocked_path_globs():
    erp = load_company(REPO_COMPANY).connectors["erp"]
    assert erp.is_blocked_path("/__admin/reset")
    assert erp.is_blocked_path("/__anything")
    assert not erp.is_blocked_path("/invoices")


def test_origin_normalisation():
    assert normalize_origin("HTTP://127.0.0.1:8101/invoices?x=1") == "http://127.0.0.1:8101"
    assert normalize_origin("https://example.com/a") == "https://example.com:443"
    company = load_company(REPO_COMPANY)
    assert company.connector_for_origin("http://127.0.0.1:8101").id == "erp"
    assert company.connector_for_origin("http://localhost:8101") is None  # different origin: not allowlisted
    assert company.connector_for_origin("http://127.0.0.1:9999") is None


def test_company_config_is_consistent():
    company = load_company(REPO_COMPANY)
    assert company.problems() == []
    assert set(company.connectors) >= {"erp", "mail", "shared_drive"}
    assert company.person("neha kulkarni").roles == ["finance_manager"]
    assert [p.name for p in company.people_with_role("finance_manager")] == ["Neha Kulkarni"]
    assert "invoice" in company.schemas


def test_handbook_chunks_on_level_two_headings():
    company = load_company(REPO_COMPANY)
    ids = [c.id for c in company.chunks()]
    assert "ap-01#5" in ids and "ap-01#0" in ids
    duplicates_step = next(c for c in company.chunks() if c.id == "ap-01#5")
    assert "duplicates" in duplicates_step.heading.lower()
    overview = parse_handbook(company.dir / "handbook" / "00-company-overview.md")
    assert overview.id == "company-overview" and overview.chunks[0].id == "company-overview#0"
    # Deliberately undocumented: the system must learn what "Acme" means from a person.
    assert not any("acme" in c.text.lower() for c in company.chunks())

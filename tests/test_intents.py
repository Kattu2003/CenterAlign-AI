"""Write-intent state machine (plan §12)."""

from __future__ import annotations

import pytest

from karya import db
from karya.config import get_settings
from karya.gateway import intents
from karya.gateway.intents import ReconcileLookup


@pytest.fixture()
def conn(tmp_path):
    connection = db.connect(tmp_path / "intents.db")
    yield connection
    connection.close()


def test_state_machine_transitions(conn):
    i = intents.create(conn, "T-1", "erp", "create_invoice", "k", {"a": "1"})
    assert intents.get(conn, i)["status"] == "pending"
    assert intents.record_response(conn, i, "unknown", 504)
    assert intents.mark_reconciled(conn, i, "committed", record_ref="ERP-INV-000104")
    row = intents.get(conn, i)
    assert row["status"] == "committed" and row["reconciled"] == 1 and row["record_ref"] == "ERP-INV-000104"
    # Terminal states do not move.
    assert not intents.mark_reconciled(conn, i, "failed")


def test_reconciliation_overrides_late_responses(conn):
    i = intents.create(conn, "T-1", "erp", "create_invoice", "k", {})
    intents.record_response(conn, i, "unknown", 504)
    intents.mark_reconciled(conn, i, "failed", note="no record")
    # A late success response arrives after reconciliation: ignored.
    assert not intents.record_response(conn, i, "committed", 303)
    assert intents.get(conn, i)["status"] == "failed"


def test_invalid_transitions_rejected(conn):
    i = intents.create(conn, "T-1", "erp", "create_invoice", "k", {})
    intents.record_response(conn, i, "committed", 303)
    assert not intents.record_response(conn, i, "failed", 500)
    assert intents.get(conn, i)["status"] == "committed"


def test_unresolved_and_lookup_by_key(conn):
    a = intents.create(conn, "T-1", "erp", "create_invoice", "k1", {})
    b = intents.create(conn, "T-1", "erp", "create_invoice", "k1", {})
    intents.record_response(conn, a, "failed", 409)
    assert [r["id"] for r in intents.find_by_idem(conn, "T-1", "k1")] == [b, a]  # newest first
    assert [r["id"] for r in intents.unresolved(conn, "T-1")] == [b]


def test_lookup_decides_exists_by_list_or_match():
    from types import SimpleNamespace

    non_empty = SimpleNamespace(exists_if="non_empty_list", match=None, record_ref_field="erp_ref")
    assert ReconcileLookup._decide(non_empty, [{"erp_ref": "ERP-INV-000104"}], {}).record_ref == "ERP-INV-000104"
    assert ReconcileLookup._decide(non_empty, [], {}).exists is False
    matcher = SimpleNamespace(exists_if=None, match={"system": "{system}", "level": "{level}"}, record_ref_field="id")
    items = [{"id": 1, "system": "erp", "level": "viewer"}, {"id": 2, "system": "mail", "level": "viewer"}]
    assert ReconcileLookup._decide(matcher, items, {"system": "erp", "level": "viewer"}).record_ref == "1"
    assert ReconcileLookup._decide(matcher, items, {"system": "erp", "level": "admin"}).exists is False

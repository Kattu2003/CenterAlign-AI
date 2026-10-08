"""Policy engine unit tests (plan §12): ops, AND, precedence, matching, fail-closed."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from karya.gateway.policy import PolicySet, evaluate, referenced_fields
from tests.conftest import REPO_COMPANY


def policies(*rules: dict) -> PolicySet:
    return PolicySet.model_validate({"version": 1, "rules": list(rules)})


def rule(rule_id: str, effect: str, when: list[dict] | None = None, connector: str = "erp", action: str = "create_invoice", **extra) -> dict:
    data = {"id": rule_id, "description": f"rule {rule_id}", "match": {"connector": connector, "action": action}, "when": when or [], "effect": effect}
    if effect == "require_approval":
        data["approver_role"] = extra.get("approver_role", "finance_manager")
    return data


@pytest.mark.parametrize(
    ("op", "value", "actual", "expected"),
    [
        ("eq", 50000, Decimal("50000.00"), True),
        ("eq", 50000, Decimal("50000.01"), False),
        ("ne", 50000, Decimal("1"), True),
        ("gt", 50000, Decimal("50000.01"), True),
        ("gt", 50000, Decimal("50000.00"), False),  # exactly at the limit: no approval
        ("gte", 50000, Decimal("50000.00"), True),
        ("lt", 100, Decimal("99.99"), True),
        ("lte", 100, Decimal("100"), True),
        ("lte", 100, Decimal("100.01"), False),
        ("in", ["admin", "editor"], "Admin", True),  # strings compare case-insensitively
        ("in", ["admin", "editor"], "viewer", False),
        ("not_in", ["admin"], "viewer", True),
        ("contains", "pre-approved", "This is PRE-APPROVED", True),
        ("contains", "urgent", "normal text", False),
        ("eq", "admin", "ADMIN", True),
    ],
)
def test_each_operator(op, value, actual, expected):
    p = policies(rule("R1", "deny", [{"field": "x", "op": op, "value": value}]))
    decision = evaluate(p, "erp", "create_invoice", {"x": actual})
    assert (decision.effect == "deny") is expected


def test_date_comparison():
    p = policies(rule("R1", "deny", [{"field": "due_date", "op": "lt", "value": "2026-10-01"}]))
    assert evaluate(p, "erp", "create_invoice", {"due_date": date(2026, 9, 30)}).effect == "deny"
    assert evaluate(p, "erp", "create_invoice", {"due_date": date(2026, 10, 1)}).effect == "allow"


def test_conditions_are_anded():
    p = policies(
        rule("R1", "require_approval", [
            {"field": "amount", "op": "gt", "value": 50000},
            {"field": "vendor_id", "op": "eq", "value": "V003"},
        ])
    )
    assert evaluate(p, "erp", "create_invoice", {"amount": Decimal("60000"), "vendor_id": "V003"}).effect == "require_approval"
    assert evaluate(p, "erp", "create_invoice", {"amount": Decimal("60000"), "vendor_id": "V001"}).effect == "allow"
    assert evaluate(p, "erp", "create_invoice", {"amount": Decimal("100"), "vendor_id": "V003"}).effect == "allow"


def test_precedence_deny_over_approval_over_allow():
    p = policies(
        rule("ALLOW", "allow"),
        rule("APPROVE", "require_approval", [{"field": "amount", "op": "gt", "value": 10}]),
        rule("DENY", "deny", [{"field": "amount", "op": "gt", "value": 1000}]),
    )
    small = evaluate(p, "erp", "create_invoice", {"amount": Decimal("5")})
    assert small.effect == "allow" and small.rule_ids == ["ALLOW"]
    medium = evaluate(p, "erp", "create_invoice", {"amount": Decimal("500")})
    assert medium.effect == "require_approval" and medium.deciding_ids == ["APPROVE"]
    assert medium.rule_ids == ["ALLOW", "APPROVE"]  # every matching rule is recorded
    large = evaluate(p, "erp", "create_invoice", {"amount": Decimal("5000")})
    assert large.effect == "deny" and large.deciding_ids == ["DENY"]
    assert set(large.rule_ids) == {"ALLOW", "APPROVE", "DENY"}


def test_matching_by_connector_and_action():
    p = policies(rule("AP-002", "deny", action="update_vendor_bank"), rule("HR", "deny", connector="hr", action="create_invoice"))
    assert evaluate(p, "erp", "update_vendor_bank", {}).effect == "deny"
    assert evaluate(p, "erp", "create_invoice", {}).effect == "allow"
    assert evaluate(p, "mail", "update_vendor_bank", {}).effect == "allow"


def test_no_rules_means_allow():
    assert evaluate(policies(), "erp", "create_invoice", {"amount": Decimal("1")}).effect == "allow"


def test_fail_closed_on_missing_or_uncoercible_field():
    p = policies(rule("AP-001", "require_approval", [{"field": "amount", "op": "gt", "value": 50000}]))
    missing = evaluate(p, "erp", "create_invoice", {})
    assert missing.effect == "require_approval" and missing.notes
    garbage = evaluate(p, "erp", "create_invoice", {"amount": "fifty thousand"})
    assert garbage.effect == "require_approval"


def test_approver_role_required_for_approval_rules():
    with pytest.raises(ValueError):
        PolicySet.model_validate({"rules": [{"id": "X", "description": "x", "match": {"connector": "erp", "action": "a"}, "effect": "require_approval"}]})


def test_referenced_fields_and_meridian_policies():
    from karya.company import load_company

    company = load_company(REPO_COMPANY)
    decision = evaluate(company.policies, "erp", "create_invoice", {"amount": Decimal("184500.00"), "vendor_id": "V003"})
    assert decision.effect == "require_approval"
    assert decision.approver_roles == ["finance_manager"]
    assert referenced_fields(decision.deciding) == {"amount"}
    assert evaluate(company.policies, "erp", "create_invoice", {"amount": Decimal("42380.00")}).effect == "allow"
    assert evaluate(company.policies, "erp", "update_vendor_bank", {"vendor_id": "V004"}).deciding_ids == ["AP-002"]
    assert evaluate(company.policies, "erp", "create_vendor", {}).deciding_ids == ["AP-003"]

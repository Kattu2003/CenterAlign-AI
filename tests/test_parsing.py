"""Deterministic normalizers and schema checks (plan §12)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from karya.tools.parsing import format_inr, parse_date, parse_money, run_checks


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Rs. 1,84,500.00", Decimal("184500.00")),
        ("INR 18900.00", Decimal("18900.00")),
        ("₹42,380", Decimal("42380.00")),
        ("INR 1,84,500.00", Decimal("184500.00")),
        ("42380", Decimal("42380.00")),
        ("Rs.9,204.00", Decimal("9204.00")),
        ("27,640.00/-", Decimal("27640.00")),
    ],
)
def test_parse_money(raw, expected):
    assert parse_money(raw) == expected


@pytest.mark.parametrize("raw", ["", "Rs. abc", "1.2.3", "forty"])
def test_parse_money_rejects_garbage(raw):
    with pytest.raises(ValueError):
        parse_money(raw)


@pytest.mark.parametrize("raw", ["30/10/2026", "30-Oct-2026", "30 Oct 2026", "2026-10-30", "30 October 2026", "Oct 30, 2026", "30.10.2026"])
def test_parse_date_formats(raw):
    assert parse_date(raw)[0] == date(2026, 10, 30)


def test_slash_dates_are_day_first_and_noted():
    value, note = parse_date("05/10/2026")
    assert value == date(2026, 10, 5) and "day-first" in note
    assert parse_date("2026-10-05")[1] is None


def test_parse_date_rejects_garbage():
    with pytest.raises(ValueError):
        parse_date("32/13/2026")
    with pytest.raises(ValueError):
        parse_date("next Tuesday")


def test_schema_checks():
    checks = [
        {"type": "sum", "parts": ["subtotal", "tax_total"], "equals": "total", "tolerance": 1.0},
        {"type": "date_order", "earlier": "invoice_date", "later": "due_date"},
    ]
    good = {"subtotal": Decimal("35915.25"), "tax_total": Decimal("6464.75"), "total": Decimal("42380.00"), "invoice_date": date(2026, 10, 5), "due_date": date(2026, 11, 4)}
    results = run_checks(checks, good, ["total"])
    assert all(r.ok for r in results)
    bad = dict(good, total=Decimal("43280.00"), due_date=date(2026, 9, 1))
    by_type = {r.type: r for r in run_checks(checks, bad, ["total"])}
    assert not by_type["sum"].ok and "differs by 900.00" in by_type["sum"].detail
    assert not by_type["date_order"].ok
    missing = run_checks(checks, dict(good, total=None), ["total"])
    assert not next(r for r in missing if r.type == "required").ok
    within = run_checks(checks, dict(good, total=Decimal("42380.90")), [])
    assert next(r for r in within if r.type == "sum").ok  # inside the INR 1 tolerance


def test_format_inr():
    assert format_inr(Decimal("184500")) == "1,84,500.00"
    assert format_inr(Decimal("42380.5")) == "42,380.50"
    assert format_inr(Decimal("999")) == "999.00"

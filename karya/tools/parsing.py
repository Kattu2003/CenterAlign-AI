"""Deterministic normalizers and generic schema checks (DECISIONS.md D16).

The LLM only copies values exactly as printed; code turns them into typed
values. "Rs. 1,84,500.00", "INR 18900.00" and "₹42,380" all become Decimals;
"30/10/2026", "30-Oct-2026", "30 Oct 2026" and "2026-10-30" all become
2026-10-30. A misread digit fails a schema check instead of silently posting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

MONTHS = {
    name: i
    for i, names in enumerate(
        [("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",), ("jun", "june"),
         ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"), ("dec", "december")],
        start=1,
    )
    for name in names
}


def parse_money(raw: str | int | float | Decimal) -> Decimal:
    """'Rs. 1,84,500.00' -> Decimal('184500.00'). Raises ValueError."""
    if isinstance(raw, Decimal):
        return raw.quantize(Decimal("0.01"))
    text = str(raw).strip()
    text = re.sub(r"(?i)^(rs\.?|inr|₹)\s*", "", text)
    text = re.sub(r"(?i)\s*(rs\.?|inr|₹|/-)$", "", text)
    text = text.replace(",", "").replace(" ", "")
    if not re.fullmatch(r"-?\d+(\.\d+)?", text):
        raise ValueError(f"not an amount: {raw!r}")
    try:
        return Decimal(text).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError(f"not an amount: {raw!r}") from exc


def parse_date(raw: str) -> tuple[date, str | None]:
    """Returns (date, note). Slash/dot/dash numeric dates are read day-first (India)."""
    text = " ".join(str(raw).strip().replace(",", " ").split())
    if match := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text):
        y, m, d = (int(g) for g in match.groups())
        return date(y, m, d), None
    if match := re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", text):
        d, m, y = (int(g) for g in match.groups())
        return date(y, m, d), f"read {raw!r} as day-first (DD/MM/YYYY)"
    if match := re.fullmatch(r"(\d{1,2})[ -]([A-Za-z]{3,9})[ -](\d{4})", text):
        d, name, y = match.groups()
        month = MONTHS.get(name.lower())
        if month:
            return date(int(y), month, int(d)), None
    if match := re.fullmatch(r"([A-Za-z]{3,9}) (\d{1,2}) (\d{4})", text):
        name, d, y = match.groups()
        month = MONTHS.get(name.lower())
        if month:
            return date(int(y), month, int(d)), None
    raise ValueError(f"not a date: {raw!r}")


def normalize(raw: Any, type_: str) -> tuple[Any, str | None]:
    """Normalize one extracted value per schema type. Raises ValueError."""
    if type_ == "decimal":
        return parse_money(raw), None
    if type_ == "date":
        return parse_date(raw)
    if type_ == "int":
        return int(str(raw).replace(",", "").strip()), None
    return " ".join(str(raw).split()), None


@dataclass
class CheckResult:
    type: str
    ok: bool
    detail: str


def run_checks(checks: list[dict[str, Any]], values: dict[str, Any], required: list[str]) -> list[CheckResult]:
    """Generic check types: sum, date_order, required. Schemas are data; no code per schema."""
    results: list[CheckResult] = []
    missing = [name for name in required if values.get(name) in (None, "")]
    results.append(CheckResult("required", not missing, "all required fields present" if not missing else f"missing: {', '.join(missing)}"))
    for check in checks:
        kind = check.get("type")
        if kind == "sum":
            parts = [values.get(p) for p in check["parts"]]
            total = values.get(check["equals"])
            if any(not isinstance(v, Decimal) for v in parts + [total]):
                results.append(CheckResult("sum", False, f"cannot check {' + '.join(check['parts'])} = {check['equals']}: values missing"))
                continue
            tolerance = Decimal(str(check.get("tolerance", 0)))
            computed = sum(parts, Decimal("0"))
            ok = abs(computed - total) <= tolerance
            results.append(CheckResult("sum", ok, f"{' + '.join(f'{p} {v}' for p, v in zip(check['parts'], parts))} = {computed} vs {check['equals']} {total}" + ("" if ok else f" (differs by {abs(computed - total)})")))
        elif kind == "date_order":
            earlier, later = values.get(check["earlier"]), values.get(check["later"])
            if not isinstance(earlier, date) or not isinstance(later, date):
                results.append(CheckResult("date_order", False, f"cannot compare {check['earlier']} and {check['later']}"))
                continue
            ok = earlier <= later
            results.append(CheckResult("date_order", ok, f"{check['earlier']} {earlier} {'<=' if ok else '>'} {check['later']} {later}"))
        elif kind == "required":
            pass  # handled above from the schema's own required flags
        else:
            results.append(CheckResult(str(kind), False, f"unknown check type {kind!r}"))
    return results


def format_inr(amount: Decimal) -> str:
    """Decimal('184500.00') -> '1,84,500.00' (Indian grouping) for human-readable messages."""
    whole, _, fraction = f"{amount:.2f}".partition(".")
    sign = "-" if whole.startswith("-") else ""
    digits = whole.lstrip("-")
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups + [tail])
    return f"{sign}{digits}.{fraction}"

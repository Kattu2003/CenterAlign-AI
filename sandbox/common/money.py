"""Money helpers. Amounts are stored as integer paise; floats never touch money."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


def to_paise(value: str | Decimal | int) -> int:
    """'42380.00' -> 4238000. Raises ValueError for anything that is not a plain number
    with at most two decimals."""
    try:
        amount = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise ValueError(f"not a number: {value!r}") from exc
    if not amount.is_finite():
        raise ValueError(f"not a number: {value!r}")
    if amount != amount.quantize(Decimal("0.01")):
        raise ValueError("at most two decimal places are allowed")
    return int((amount * 100).to_integral_value(rounding=ROUND_HALF_UP))


def paise_to_decimal(paise: int) -> Decimal:
    return (Decimal(paise) / 100).quantize(Decimal("0.01"))


def paise_to_str(paise: int) -> str:
    """4238000 -> '42380.00' (the API format)."""
    return f"{paise_to_decimal(paise):.2f}"


def indian_group(whole: int) -> str:
    """184500 -> '1,84,500' (Indian digit grouping: 3 digits, then groups of 2)."""
    digits = str(abs(whole))
    if len(digits) <= 3:
        grouped = digits
    else:
        head, tail = digits[:-3], digits[-3:]
        pairs = []
        while len(head) > 2:
            pairs.insert(0, head[-2:])
            head = head[:-2]
        if head:
            pairs.insert(0, head)
        grouped = ",".join(pairs + [tail])
    return ("-" if whole < 0 else "") + grouped


def format_inr(paise: int, prefix: str = "") -> str:
    """4238000 -> '42,380.00'; with prefix 'INR ' -> 'INR 42,380.00'."""
    whole, fraction = divmod(abs(paise), 100)
    sign = "-" if paise < 0 else ""
    return f"{prefix}{sign}{indian_group(whole)}.{fraction:02d}"

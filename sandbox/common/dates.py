"""Relative dates for seed data.

All seed dates are relative to D = today in Asia/Kolkata at reset time, so
"the latest invoice" is meaningful whenever the demo runs. India has no DST, so
a fixed +05:30 offset is exact and avoids needing the tzdata package on Windows.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

_TOKEN = re.compile(r"^D\s*(?:([+-])\s*(\d+))?$")


def today_ist() -> date:
    return datetime.now(IST).date()


def resolve(token: str | int, base: date) -> date:
    """'D-3' -> base - 3 days; 'D+27' -> base + 27; 'D' -> base."""
    match = _TOKEN.match(str(token).strip())
    if not match:
        raise ValueError(f"not a relative date token: {token!r}")
    sign, days = match.groups()
    offset = int(days or 0) * (-1 if sign == "-" else 1)
    return base + timedelta(days=offset)

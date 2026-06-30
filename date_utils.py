"""
Parse relative support policies and compute estimated calendar dates.

Only computes estimates when BOTH release_date AND an explicit relative policy
are present — never guesses release dates.
"""

import re
from datetime import datetime
from typing import Optional, Tuple


_RELATIVE_PATTERN = re.compile(
    r"(\d+)\s*(year|years|yr|yrs|month|months|mo|mos)\b",
    re.IGNORECASE,
)


def parse_iso_date(date_str: str) -> Optional[datetime]:
    """Parse YYYY, YYYY-MM, or YYYY-MM-DD."""
    if not date_str:
        return None
    s = date_str.strip()
    try:
        parts = s.split("-")
        y = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 1
        d = int(parts[2]) if len(parts) > 2 else 1
        return datetime(y, m, min(d, 28))
    except (ValueError, IndexError):
        return None


def format_iso_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def parse_relative_policy(policy_text: str) -> Optional[Tuple[int, str]]:
    if not policy_text:
        return None
    m = _RELATIVE_PATTERN.search(policy_text)
    if not m:
        return None
    amount = int(m.group(1))
    unit = m.group(2).lower()
    if unit.startswith("y") or unit in ("yr", "yrs"):
        return amount, "years"
    return amount, "months"


def add_policy_to_release(release: datetime, amount: int, unit: str) -> datetime:
    y, m, d = release.year, release.month, release.day
    if unit == "years":
        return datetime(y + amount, m, d)
    # months
    total_months = y * 12 + (m - 1) + amount
    new_y = total_months // 12
    new_m = total_months % 12 + 1
    return datetime(new_y, new_m, d)


def compute_estimated_end_of_support(
    release_date: Optional[str],
    support_policy_relative: Optional[str],
) -> Tuple[Optional[str], str]:
    if support_policy_relative and not release_date:
        return None, "relative_only"
    if not support_policy_relative or not release_date:
        return None, "exact"

    parsed = parse_relative_policy(support_policy_relative)
    release_dt = parse_iso_date(release_date)
    if not parsed or not release_dt:
        if support_policy_relative:
            return None, "relative_only"
        return None, "exact"

    amount, unit = parsed
    return format_iso_date(add_policy_to_release(release_dt, amount, unit)), "estimated"


def merge_date_fields(data: dict) -> dict:
    """Add estimated_end_of_support and date_precision to extraction dict."""
    release = data.get("release_date")
    policy = data.get("support_policy_relative")
    explicit_eos = data.get("end_of_support")

    estimated, precision = compute_estimated_end_of_support(release, policy)

    if explicit_eos:
        data["date_precision"] = data.get("date_precision") or "exact"
    elif estimated:
        data["estimated_end_of_support"] = estimated
        data["date_precision"] = "estimated"
    elif policy:
        data["date_precision"] = "relative_only"

    return data

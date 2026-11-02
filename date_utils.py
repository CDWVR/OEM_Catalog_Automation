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


# ─── Grounding: does an extracted date actually appear in the source text? ───

_MONTH_NAMES = [
    ("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"),
    ("may", "may"), ("june", "jun"), ("july", "jul"), ("august", "aug"),
    ("september", "sept?"), ("october", "oct"), ("november", "nov"), ("december", "dec"),
]

_ISO_LIKE = re.compile(r"^\s*(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?\s*$")


def date_grounding(date_str: Optional[str], text: str) -> str:
    """
    How well `date_str` (YYYY, YYYY-MM or YYYY-MM-DD) is supported by `text`:
      "exact"   — the date (at its stated precision) appears in some common format
      "partial" — the year appears, but not the month/day in a recognizable format
      "absent"  — the year does not appear at all (date was not on the page)
    """
    if not date_str or not text:
        return "absent"
    m = _ISO_LIKE.match(str(date_str))
    if not m:
        return "absent"
    year, month, day = m.group(1), m.group(2), m.group(3)
    t = text.lower()
    if not re.search(rf"(?<!\d){year}(?!\d)", t):
        return "absent"
    if not month:
        return "exact"

    mo = int(month)
    if not 1 <= mo <= 12:
        return "partial"
    full, abbr = _MONTH_NAMES[mo - 1]
    name = rf"(?:{full}|{abbr}\.?)"
    mo_num = rf"0?{mo}"
    sep = r"[-/.]"
    ord_sfx = r"(?:st|nd|rd|th)?"

    if day:
        d = int(day)
        d_num = rf"0?{d}"
        patterns = [
            rf"{year}{sep}{mo_num}{sep}{d_num}(?!\d)",                       # 2024-10-08, 2024/10/8
            rf"(?<!\d){mo_num}{sep}{d_num}{sep}{year}",                      # 10/08/2024
            rf"(?<!\d){d_num}{sep}{mo_num}{sep}{year}",                      # 08/10/2024, 08.10.2024
            rf"\b{name}\s+{d_num}{ord_sfx},?\s+{year}",                      # October 8, 2024
            rf"(?<!\d){d_num}{ord_sfx}\s+(?:of\s+)?{name},?\s+{year}",       # 8 October 2024
            rf"(?<!\d){d_num}{sep}{name}{sep}{year}",                        # 08-Oct-2024
        ]
        if any(re.search(p, t) for p in patterns):
            return "exact"
        return "partial"

    patterns = [
        rf"{year}{sep}{mo_num}(?!\d)",                                       # 2024-10
        rf"(?<!\d){mo_num}{sep}{year}",                                      # 10/2024
        rf"\b{name}\s+(?:\d{{1,2}}{ord_sfx},?\s+)?{year}",                   # October 2024
        rf"\b{name}{sep}{year}",                                             # Oct-2024
        rf"(?<!\d)\d{{1,2}}{ord_sfx}\s+{name},?\s+{year}",                   # 8 October 2024
    ]
    if any(re.search(p, t) for p in patterns):
        return "exact"
    return "partial"

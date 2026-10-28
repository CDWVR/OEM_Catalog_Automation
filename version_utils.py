"""Extract version tokens from product names without false positives."""

import re
from typing import Optional

# Trailing version forms, most specific first. Each pattern's group 1 is the version.
_YEAR_RELEASE = re.compile(r"\s+((?:19|20)\d{2}\s+R\d)\s*$", re.IGNORECASE)          # Windows Server 2012 R2
_ORACLE_SUFFIX = re.compile(r"\s+(\d{1,2})[cgi]\s*$", re.IGNORECASE)                 # Oracle Database 19c / 11g / 9i
_DOTTED = re.compile(r"\s+v?(\d+\.\d+(?:\.\d+)*)\s*$", re.IGNORECASE)                # vSphere 7.0
_YEAR = re.compile(r"\s+((?:19|20)\d{2})\s*$")                                       # SQL Server 2019
_SMALL = re.compile(r"\s+v?(\d{1,2})\s*$", re.IGNORECASE)                            # RHEL 8, Java SE 8

# Same forms, matched whole, for stripping the version off a name.
_STRIP = re.compile(
    r"\s+(?:(?:19|20)\d{2}\s+R\d|\d{1,2}[cgi]|v?\d+(?:\.\d+)*)\s*$",
    re.IGNORECASE,
)


def extract_version(product_name: str) -> Optional[str]:
    if not product_name:
        return None
    name = product_name.strip()
    if re.search(r"\boffice\s+\d+\s*$", name, re.IGNORECASE):
        return None
    m = _YEAR_RELEASE.search(name)
    if m:
        year, release = m.group(1).split()
        return f"{year} {release.upper()}"
    for pattern in (_ORACLE_SUFFIX, _DOTTED, _YEAR, _SMALL):
        m = pattern.search(name)
        if m:
            return m.group(1)
    return None


def strip_trailing_version(product_name: str) -> str:
    """Product name without its trailing version token ('Windows Server 2012 R2' -> 'Windows Server')."""
    return re.sub(r"\s+", " ", _STRIP.sub("", product_name.strip())).strip()


def mentions_version(product_name: str) -> bool:
    """True when the name carries a version-like token, parsed or not.

    Used to refuse 'latest version' fallbacks: if the user named a version we failed to parse,
    answering with the newest release's dates would be a confident wrong answer.
    """
    return bool(re.search(r"\d", product_name or ""))


def normalize_version(version: str) -> str:
    """Comparable form: '2012 R2' -> '2012-r2', '15.0' -> '15.0'."""
    return re.sub(r"\s+", "-", version.strip().lower())

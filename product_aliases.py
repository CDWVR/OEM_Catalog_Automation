"""
Product / OEM aliases — improve search and page matching when catalog names differ
from OEM site terminology (e.g. 'Alias Design 2021' vs 'Autodesk Alias').
"""

import re
from typing import Optional

# (oem_substring, product_pattern) -> list of alternate search phrases (lowercase keys on OEM)
# product_pattern is matched case-insensitively as substring of query product name
_PRODUCT_ALIASES: list[tuple[str, str, list[str]]] = [
    ("autodesk", "alias", ["Autodesk Alias", "Alias", "Alias Design", "Alias Design Studio"]),
    ("autodesk", "inventor", ["Autodesk Inventor", "Inventor"]),
    ("autodesk", "maya", ["Autodesk Maya", "Maya"]),
    ("autodesk", "3ds max", ["3ds Max", "Autodesk 3ds Max"]),
    ("microsoft", "windows server", ["Windows Server", "Microsoft Windows Server"]),
    ("microsoft", "sql server", ["SQL Server", "Microsoft SQL Server"]),
    ("ibm", "websphere application server", [
        "WebSphere Application Server", "WAS", "IBM WebSphere",
    ]),
    ("oracle", "database", ["Oracle Database", "Oracle DB"]),
    ("vmware", "vsphere", ["VMware vSphere", "vSphere", "ESXi"]),
    ("bmc", "control-m", ["Control-M", "BMC Control-M", "Control M"]),
    ("red hat", "rhel", ["Red Hat Enterprise Linux", "RHEL", "Red Hat Linux"]),
    ("cisco", "ios", ["Cisco IOS", "IOS XE"]),
]


def _oem_matches(oem: str, pattern: str) -> bool:
    return pattern in oem.lower()


def get_aliases(product_name: str, oem: str) -> list[str]:
    """Alternate names for search and matching (deduped, includes original)."""
    product_lower = product_name.lower()
    oem_lower = oem.lower()
    out: list[str] = [product_name.strip()]
    seen = {normalize_key(product_name)}

    for oem_pat, prod_pat, aliases in _PRODUCT_ALIASES:
        if oem_pat not in oem_lower and oem_pat not in product_lower:
            continue
        if prod_pat not in product_lower:
            continue
        for alias in aliases:
            key = normalize_key(alias)
            if key not in seen:
                seen.add(key)
                out.append(alias)

    base = re.sub(r"\s+\d{4}\s*$", "", product_name).strip()
    if base and normalize_key(base) not in seen:
        out.append(base)
        seen.add(normalize_key(base))

    return out


def normalize_key(name: str) -> str:
    return re.sub(r"\s+", " ", name.lower().strip())


def get_search_name_variants(product_name: str, oem: str, max_variants: int = 4) -> list[str]:
    """Names to use in web search (original first)."""
    aliases = get_aliases(product_name, oem)
    return aliases[:max_variants]


def alias_hint_for_prompt(product_name: str, oem: str) -> str:
    """Short line for Gemini when the page may use a different product label."""
    al = get_aliases(product_name, oem)
    if len(al) <= 1:
        return ""
    others = [a for a in al if normalize_key(a) != normalize_key(product_name)]
    if not others:
        return ""
    return (
        "Known alternate names on OEM sites: "
        + ", ".join(others[:5])
        + ". Treat these as the same product line if the page uses them."
    )

"""
Resolve product version when the user query omits one.

Strategy (in order):
  1. Version already in product name (e.g. 'Windows Server 2019')
  2. endoflife.date latest cycle for known products
  3. None — broad search using base product name only
"""

import re
from dataclasses import dataclass
from typing import Optional

from sources.endoflife_api import get_latest_cycle_version
from version_utils import extract_version


@dataclass
class PreparedQuery:
    """Normalized query passed through the engine pipeline."""
    product_name: str          # Original user input
    oem: str
    base_name: str             # Product name without version token
    version: Optional[str]     # Resolved or extracted version
    version_auto_resolved: bool
    effective_name: str        # Name used for search/extraction (may include version)


def strip_version_from_name(product_name: str, version: Optional[str]) -> str:
    if not version:
        return product_name.strip()
    pattern = re.compile(r"\b" + re.escape(version) + r"\b", re.IGNORECASE)
    base = pattern.sub("", product_name)
    return re.sub(r"\s+", " ", base).strip()


def prepare_query(product_name: str, oem: str, verbose: bool = True) -> PreparedQuery:
    """
    If no version in the query, attempt to resolve the latest via endoflife.date.
    """
    product_name = product_name.strip()
    oem = oem.strip()
    version = extract_version(product_name)
    version_auto = False

    if not version:
        latest = get_latest_cycle_version(product_name)
        if latest:
            version = latest
            version_auto = True
            if verbose:
                print(f"  [i] No version in query — using latest: v{version} (endoflife.date)")

    base_name = strip_version_from_name(product_name, version) or product_name
    if version and version not in product_name:
        effective = f"{base_name} {version}".strip()
    else:
        effective = product_name

    return PreparedQuery(
        product_name=product_name,
        oem=oem,
        base_name=base_name,
        version=version,
        version_auto_resolved=version_auto,
        effective_name=effective,
    )

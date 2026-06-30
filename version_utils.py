"""Extract version tokens from product names without false positives."""

import re
from typing import Optional


def extract_version(product_name: str) -> Optional[str]:
    if not product_name:
        return None
    name = product_name.strip()
    if re.search(r"\boffice\s+\d+\s*$", name, re.IGNORECASE):
        return None
    m = re.search(r"\s+v?(\d+\.\d+(?:\.\d+)*)\s*$", name, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"\s+((?:19|20)\d{2})\s*$", name)
    if m:
        return m.group(1)
    m = re.search(r"\s+v?(\d{1,2})\s*$", name, re.IGNORECASE)
    if m:
        return m.group(1)
    return None

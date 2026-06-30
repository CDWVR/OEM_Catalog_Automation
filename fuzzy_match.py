"""Boundary-aware registry matching (avoids 'java' matching 'javascript')."""

import re
from typing import Optional


def normalize_key(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())


def _phrase_boundary_pattern(phrase: str) -> re.Pattern:
    escaped = re.escape(phrase.strip())
    escaped = escaped.replace(r"\ ", r"\s+")
    return re.compile(r"(?:^|\b)" + escaped + r"(?:\b|$)", re.IGNORECASE)


def phrase_matches(phrase: str, text: str) -> bool:
    if not phrase or not text:
        return False
    p = normalize_key(phrase)
    t = normalize_key(text)
    if p == t:
        return True
    return bool(_phrase_boundary_pattern(p).search(t))


def match_registry_key(name: str, registry: dict) -> Optional[str]:
    normalized = normalize_key(name)
    if not normalized:
        return None
    if normalized in registry:
        return normalized
    best_key: Optional[str] = None
    best_len = 0
    for key in registry:
        k = normalize_key(key)
        if phrase_matches(k, normalized) or phrase_matches(normalized, k):
            if len(k) > best_len:
                best_key = k
                best_len = len(k)
    return best_key


def match_registry_value(name: str, registry: dict) -> Optional[str]:
    key = match_registry_key(name, registry)
    return registry.get(key) if key else None

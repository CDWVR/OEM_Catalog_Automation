"""
Product name matching — distinguish similar / nested product names.

Problem: substring counts treat
  "WebSphere Application Server"
as a hit inside
  "WebSphere Application Server Network Deployment"

This module uses boundary-aware matching and detects longer "superset"
product names on the same page.
"""

import re
from typing import Optional


def normalize_product_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.lower().strip())


def edition_core(name: str) -> str:
    """Product edition name without trailing version token (e.g. 'WAS 9.0' -> 'was')."""
    n = normalize_product_name(name)
    return re.sub(r"\s+v?\d+(?:\.\d+)*\s*$", "", n).strip()


def _phrase_pattern(phrase: str) -> re.Pattern:
    """Regex for phrase with flexible whitespace, case-insensitive."""
    escaped = re.escape(phrase.strip())
    escaped = escaped.replace(r"\ ", r"\s+")
    return re.compile(escaped, re.IGNORECASE)


def count_boundary_phrase(text: str, phrase: str, skip_if_superset_prefix: bool = True) -> int:
    """
    Count phrase occurrences at word boundaries.
    If skip_if_superset_prefix: do not count when the match starts a longer product
    name (e.g. skip WAS when text says WAS Network Deployment at that position).
    """
    if not phrase or not text:
        return 0
    supersets = find_superset_product_names(phrase, text) if skip_if_superset_prefix else []
    pat = _phrase_pattern(phrase)
    count = 0
    for m in pat.finditer(text):
        start, end = m.start(), m.end()
        before_ok = start == 0 or not text[start - 1].isalnum()
        after_ok = end == len(text) or not text[end].isalnum()
        if not (before_ok and after_ok):
            continue
        if supersets:
            snippet = normalize_product_name(text[start : start + max(len(s) for s in supersets) + 5])
            if any(snippet.startswith(s) for s in supersets):
                continue
        count += 1
    return count


_STOP_SUFFIX_WORDS = frozenset({
    "eos", "eol", "eol", "end", "of", "support", "life", "is", "are", "was", "were",
    "the", "a", "an", "for", "from", "on", "at", "by", "with", "date", "dates",
})


def find_superset_product_names(query: str, text: str, max_extra_words: int = 5) -> list[str]:
    """
    Find longer product names in text that start with the query phrase.
    E.g. query='websphere application server' ->
         'websphere application server network deployment'
    """
    query = normalize_product_name(query)
    if len(query) < 4:
        return []

    pattern = re.compile(
        re.escape(query) + r"((?:\s+[\w][\w.\-]*){1," + str(max_extra_words) + r"})",
        re.IGNORECASE,
    )
    found = []
    seen = set()
    for m in pattern.finditer(text):
        suffix = m.group(1).strip()
        words = []
        for w in suffix.split():
            wl = w.lower().strip(".,;:")
            if wl in _STOP_SUFFIX_WORDS or re.match(r"^\d", wl):
                break
            words.append(wl)
        if not words:
            continue
        span = query + " " + " ".join(words)
        if span != query and span not in seen:
            seen.add(span)
            found.append(span)
    return found


def score_product_mentions(
    text: str,
    product_name: str,
    base_name: Optional[str] = None,
) -> dict:
    """
    Score how well page text matches the requested product vs sibling names.

    Returns dict with:
      exact_hits, base_hits, superset_names, superset_hits, ambiguous, relevance_score
    """
    text_norm = text  # matching is case-insensitive via regex
    product = normalize_product_name(product_name)
    base = normalize_product_name(base_name) if base_name else product

    exact_hits = count_boundary_phrase(text_norm, product)
    base_hits = 0
    if base != product:
        base_hits = count_boundary_phrase(text_norm, base)

    supersets = find_superset_product_names(product, text_norm)
    superset_hits = sum(count_boundary_phrase(text_norm, s) for s in supersets)

    # Ambiguous when sibling edition appears on same page as the base product
    ambiguous = bool(supersets) and superset_hits > 0 and exact_hits > 0

    relevance = exact_hits * 10 + base_hits * 3
    if ambiguous:
        relevance = max(0, relevance - superset_hits * 8)

    return {
        "exact_hits": exact_hits,
        "base_hits": base_hits,
        "superset_names": supersets,
        "superset_hits": superset_hits,
        "ambiguous": ambiguous,
        "relevance_score": relevance,
    }


def products_match(requested: str, found: Optional[str]) -> bool:
    """True if found product name is the same edition as requested (not a sibling superset)."""
    if not found:
        return False
    req = edition_core(requested)
    fnd = edition_core(found)
    if req == fnd:
        return True
    # Page names a longer edition (e.g. ND when user asked for base WAS)
    if fnd.startswith(req + " "):
        return False
    # User asked for longer edition; page names shorter sibling only
    if req.startswith(fnd + " "):
        return False
    return False

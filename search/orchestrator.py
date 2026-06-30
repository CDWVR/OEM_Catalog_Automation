"""
Layer 3 URL discovery orchestrator.

Order (SEARCH_PROVIDER=auto):
  1. SQLite search_cache
  2. discovered_urls (crawl DB — free)
  3. Exa API (if EXA_API_KEY set) — primary web search
  4. DuckDuckGo / ddgs — fallback only

Set SEARCH_PROVIDER=exa to require Exa; discovered_only to skip web search.
Docs: https://docs.exa.ai/reference/search-api-guide-for-coding-agents
"""

import os
from typing import List
from urllib.parse import urlparse

import db
from product_aliases import get_search_name_variants
from seed_data import get_oem_domains
from search.providers import (
    DuckDuckGoProvider,
    ExaSearchProvider,
    get_exa_provider,
    get_provider,
    is_oem_url,
)


def _cache_ttl_hours() -> int:
    try:
        return int(os.getenv("SEARCH_CACHE_TTL_HOURS", "168"))
    except ValueError:
        return 168


def _exa_num_results() -> int:
    try:
        return min(100, max(3, int(os.getenv("EXA_NUM_RESULTS", "10"))))
    except ValueError:
        return 10


def _max_oem_queries() -> int:
    try:
        return max(1, int(os.getenv("SEARCH_MAX_OEM_QUERIES", "3")))
    except ValueError:
        return 3


def _max_open_queries() -> int:
    try:
        return max(0, int(os.getenv("SEARCH_MAX_OPEN_QUERIES", "1")))
    except ValueError:
        return 1


def _urls_from_provider_results(
    results: list[dict],
    oem_domains: list[str],
    max_results: int,
) -> list[str]:
    """Prefer OEM-domain URLs; fall back to external if none match."""
    oem_urls: list[str] = []
    external: list[str] = []
    seen: set[str] = set()
    for r in results:
        href = r.get("href", "")
        if not href or not href.startswith("http") or href in seen:
            continue
        seen.add(href)
        if is_oem_url(href, oem_domains):
            oem_urls.append(href)
        else:
            external.append(href)
    oem_urls.sort(key=_score_url_lifecycle_relevance, reverse=True)
    external.sort(key=_score_url_lifecycle_relevance, reverse=True)
    if oem_urls:
        return oem_urls[:max_results]
    return external[:max_results]


def _search_with_exa(
    exa: ExaSearchProvider,
    product_name: str,
    oem: str,
    oem_domains: list[str],
    max_results: int,
) -> list[str]:
    """Single Exa lifecycle search (replaces many DDGS site: queries)."""
    n = min(max_results, _exa_num_results())
    print(f"    -> [EXA] deep search ({n} results, domains={len(oem_domains or [])})...")
    raw = exa.search_lifecycle(product_name, oem, oem_domains, max_results=n)
    urls = _urls_from_provider_results(raw, oem_domains, max_results)
    if urls:
        print(f"    -> [EXA] {len(urls)} URLs")
    return urls


def _run_ddgs_queries(
    product_name: str,
    oem: str,
    oem_domains: list[str],
    max_results: int,
) -> list[str]:
    """Legacy multi-query DDGS path (fallback)."""
    import time
    ddgs = DuckDuckGoProvider()
    seen: set[str] = set()
    oem_urls: list[str] = []

    queries: list[str] = []
    if oem_domains:
        primary = oem_domains[0]
        queries.extend([
            f'site:{primary} "{product_name}" end of support OR end of life',
            f'site:{primary} "{product_name}" lifecycle support policy',
            f'site:{primary} "{product_name}"',
        ][: _max_oem_queries()])
    queries.extend([
        f'"{oem}" "{product_name}" end of support OR end of life',
    ][: _max_open_queries()])

    for q in queries:
        if len(oem_urls) >= max_results:
            break
        print(f"    -> [DDGS] {q[:70]}...")
        for r in ddgs.search(q, max_results=5):
            href = r.get("href", "")
            if href and href.startswith("http") and href not in seen:
                seen.add(href)
                if is_oem_url(href, oem_domains):
                    oem_urls.append(href)
        time.sleep(1.5)

    return oem_urls[:max_results]


_LIFECYCLE_PATH_KEYWORDS = (
    "lifecycle",
    "end-of-life",
    "end-of-support",
    "eol",
    "eos",
    "support-policy",
    "support-matrix",
    "supported-versions",
    "retired",
    "support/lifecycle",
    "lifetime-support",
    "product-support-lifecycle",
)

_PENALTY_PATH_KEYWORDS = (
    "admin",
    "adminguide",
    "devnet",
    "developer",
    "api-reference",
    "whatsnew",
    "whatsnewdc",
    "releasenotes",
    "release-notes",
    "changelog",
    "download",
    "tutorial",
    "howto",
    "getting-started",
)


def _score_url_lifecycle_relevance(url: str) -> int:
    """Higher score = more likely a lifecycle/support documentation URL."""
    path = urlparse(url).path.lower()
    score = 0
    if any(kw in path for kw in _LIFECYCLE_PATH_KEYWORDS):
        score += 20
    if "support" in path:
        score += 10
    if "product" in path:
        score += 5
    if any(kw in path for kw in _PENALTY_PATH_KEYWORDS):
        score -= 30
    return score


def search_product_lifecycle(product_name: str, oem: str, max_results: int = 20) -> List[str]:
    """
    Find candidate lifecycle URLs for a product.
    """
    provider_mode = os.getenv("SEARCH_PROVIDER", "auto").lower().strip()
    ttl = _cache_ttl_hours()

    cached = db.get_search_cache(oem, product_name, "combined", ttl_hours=ttl)
    if cached:
        print(f"    -> [CACHE] {len(cached)} URLs (TTL {ttl}h)")
        return cached[:max_results]

    oem_domains = get_oem_domains(oem)
    combined: list[str] = []

    # --- 1. Crawl database ---
    if provider_mode in ("auto", "discovered_only", "discovered_db"):
        db_urls = db.rank_discovered_urls_for_product(oem, product_name, limit=max_results)
        if db_urls:
            print(f"    -> [CRAWL-DB] {len(db_urls)} URLs from discovered_urls")
            combined.extend(db_urls)
            if len(combined) >= max_results and provider_mode == "discovered_only":
                db.save_search_cache(oem, product_name, "combined", combined[:max_results])
                return combined[:max_results]

    # --- 2. Exa (primary web search) ---
    use_exa = provider_mode in ("auto", "exa")
    if use_exa and len(combined) < max_results:
        exa = get_exa_provider()
        if exa:
            needed = max_results - len(combined)
            found = _search_with_exa(exa, product_name, oem, oem_domains, needed)
            for u in found:
                if u not in combined:
                    combined.append(u)
            for variant in get_search_name_variants(product_name, oem)[1:3]:
                if len(combined) >= max_results:
                    break
                extra = _search_with_exa(
                    exa, variant, oem, oem_domains, max_results - len(combined),
                )
                for u in extra:
                    if u not in combined:
                        combined.append(u)
        elif provider_mode == "exa":
            print("  [X] SEARCH_PROVIDER=exa but EXA_API_KEY is not set (or exa-py missing).")

    # --- 3. DDGS fallback ---
    skip_ddgs = provider_mode == "auto" and len(combined) >= max(5, max_results // 2)
    if provider_mode in ("auto", "ddgs") and len(combined) < max_results and not skip_ddgs:
        if provider_mode == "auto" and get_exa_provider() and len(combined) >= 3:
            print(f"    -> [DDGS] Skipped — Exa/crawl already has {len(combined)} URLs")
        else:
            needed = max_results - len(combined)
            found = _run_ddgs_queries(product_name, oem, oem_domains, needed)
            for u in found:
                if u not in combined:
                    combined.append(u)
    elif skip_ddgs:
        print(f"    -> [DDGS] Skipped — crawl DB has {len(combined)} URLs")

    # --- Legacy providers (explicit only) ---
    if provider_mode == "searxng" and len(combined) < max_results:
        searx = get_provider("searxng")
        if searx:
            raw = searx.search(
                f'{oem} "{product_name}" lifecycle end of support', max_results=10,
            )
            for u in _urls_from_provider_results(raw, oem_domains, max_results):
                if u not in combined:
                    combined.append(u)

    if provider_mode == "brave" and len(combined) < max_results:
        brave = get_provider("brave")
        if brave:
            raw = brave.search(f'{oem} {product_name} lifecycle', max_results=10)
            for u in _urls_from_provider_results(raw, oem_domains, max_results):
                if u not in combined:
                    combined.append(u)

    if not combined:
        print(f"    -> No URLs found for '{product_name}' / '{oem}'.")
        print("       Tip: set EXA_API_KEY, run crawl --all, or check OEM in seed_data.")
        return []

    db.save_search_cache(oem, product_name, "combined", combined)
    return combined[:max_results]

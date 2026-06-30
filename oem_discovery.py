"""
Autonomous OEM discovery — find official lifecycle hubs without pre-seeded URLs.

When an OEM is missing from seed_data (or has no hubs), search the web for
official support/lifecycle documentation, infer trusted domains, and register
hubs in SQLite for crawl + query layers.
"""

import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import db
from search.providers import DuckDuckGoProvider, get_exa_provider

# URL path/title signals for lifecycle documentation
_LIFECYCLE_URL_PATTERNS = [
    r"lifecycle",
    r"end-of-life",
    r"end-of-support",
    r"endoflife",
    r"eol",
    r"eos",
    r"lifetime-support",
    r"life-cycle",
    r"product-life",
    r"support-policy",
    r"support/lifecycle",
    r"support-matrix",
    r"supported-versions",
    r"previous-versions",
    r"product-support",
    r"support-dates",
    r"retired-products",
    r"support/resources",
]

# Hosts that are never official OEM lifecycle sources
_BLOCKLIST_HOSTS = frozenset({
    "wikipedia.org", "wikimedia.org", "reddit.com", "stackoverflow.com",
    "stackexchange.com", "youtube.com", "linkedin.com", "facebook.com",
    "twitter.com", "x.com", "medium.com", "scribd.com", "slideshare.net",
    "amazon.com", "ebay.com", "endoflife.date", "redditstatic.com",
})

_SKIP_PATH_PATTERNS = [
    r"/login", r"/careers", r"/jobs", r"/blog/", r"/news/", r"/press/",
    r"/privacy", r"/cookie", r"/legal", r"/contact", r"/community/",
    r"/forum", r"/download", r"/cart", r"/shop",
]


@dataclass
class OemDiscoveryResult:
    oem: str
    domains_added: list[str] = field(default_factory=list)
    hubs_added: list[str] = field(default_factory=list)
    search_urls_seen: int = 0
    provider: str = ""


def _classify_hub_url(url: str) -> str:
    path = urlparse(url).path.lower()
    if any(re.search(p, path) for p in (r"product-hub", r"products/", r"product-matrix", r"eligible")):
        return "product_hub"
    if re.search(r"licensing|/license", path):
        return "licensing"
    return "lifecycle"


def _url_lifecycle_score(url: str, title: str = "") -> int:
    """Higher = more likely an official lifecycle hub page."""
    parsed = urlparse(url.lower())
    host = parsed.netloc
    if any(host == b or host.endswith("." + b) for b in _BLOCKLIST_HOSTS):
        return -100
    path = parsed.path
    for pat in _SKIP_PATH_PATTERNS:
        if re.search(pat, path):
            return -50
    score = 0
    blob = f"{path} {title.lower()}"
    for pat in _LIFECYCLE_URL_PATTERNS:
        if re.search(pat, blob):
            score += 12
    if re.search(r"support|product", path):
        score += 4
    if path.count("/") <= 4:
        score += 2
    return score


def _infer_domains_from_urls(urls: list[str], oem_name: str) -> list[str]:
    """
    Build trusted domain list from discovered URLs (registrable + hostnames).
    Prefer hosts that appear on multiple lifecycle-scored URLs.
    """
    host_counts: dict[str, int] = {}
    registrable_counts: dict[str, int] = {}

    oem_tokens = [t for t in re.findall(r"[a-z0-9]+", oem_name.lower()) if len(t) > 2]

    for url in urls:
        host = urlparse(url).netloc.lower()
        if not host or host in _BLOCKLIST_HOSTS:
            continue
        if host.startswith("www."):
            host = host[4:]
        host_counts[host] = host_counts.get(host, 0) + 1

        parts = host.split(".")
        if len(parts) >= 2:
            reg = ".".join(parts[-2:])
            if reg not in _BLOCKLIST_HOSTS:
                registrable_counts[reg] = registrable_counts.get(reg, 0) + 1

    ranked_reg = sorted(registrable_counts.items(), key=lambda x: -x[1])
    ranked_host = sorted(host_counts.items(), key=lambda x: -x[1])

    domains: list[str] = []
    seen: set[str] = set()

    def add(d: str):
        if d and d not in seen and d not in _BLOCKLIST_HOSTS:
            seen.add(d)
            domains.append(d)

    for reg, count in ranked_reg[:5]:
        if count >= 1 and any(tok in reg for tok in oem_tokens):
            add(reg)
    if not domains:
        for reg, _ in ranked_reg[:3]:
            add(reg)

    for host, count in ranked_host[:8]:
        if count >= 1:
            add(host)

    return domains


def _search_oem_discovery(oem: str, product_name: Optional[str], max_results: int) -> tuple[list[dict], str]:
    """Run Exa then DDGS without requiring known OEM domains."""
    results: list[dict] = []
    provider_used = ""

    product_bit = f' "{product_name}"' if product_name else ""
    queries = [
        f"{oem}{product_bit} official product support lifecycle end of support documentation",
        f"{oem} product support policy lifetime support matrix site",
        f"{oem} end of life end of support official support portal",
    ]

    exa = get_exa_provider()
    if exa:
        provider_used = "exa"
        for q in queries:
            if len(results) >= max_results:
                break
            batch = exa.search(q, max_results=min(15, max_results))
            seen = {r["href"] for r in results}
            for r in batch:
                if r["href"] not in seen:
                    results.append(r)
                    seen.add(r["href"])
            time.sleep(0.5)

    if len(results) < max(3, max_results // 2):
        ddgs = DuckDuckGoProvider()
        provider_used = provider_used + "+ddgs" if provider_used else "ddgs"
        for q in queries:
            if len(results) >= max_results:
                break
            for r in ddgs.search(q, max_results=10):
                href = r.get("href", "")
                if href and href.startswith("http"):
                    results.append({"href": href, "title": r.get("title", "")})
            time.sleep(1.2)

    return results[:max_results], provider_used


def discover_oem_hubs(
    oem: str,
    product_name: Optional[str] = None,
    max_hubs: int = 8,
    verbose: bool = True,
) -> OemDiscoveryResult:
    """
    Search for official lifecycle URLs and score hub candidates.
    Does not write to DB — use discover_and_register_oem().
    """
    oem = oem.strip()
    if verbose:
        print(f"\n  [OEM Discovery] Searching for official '{oem}' lifecycle sources...")

    search_hits, provider_used = _search_oem_discovery(oem, product_name, max_results=25)
    scored: list[tuple[int, str, str]] = []
    all_urls: list[str] = []

    for item in search_hits:
        url = item.get("href", "")
        title = item.get("title", "") or ""
        if not url.startswith("http"):
            continue
        all_urls.append(url)
        score = _url_lifecycle_score(url, title)
        if score > 0:
            scored.append((score, url, title))

    scored.sort(key=lambda x: x[0], reverse=True)
    hub_candidates = [url for _, url, _ in scored[:max_hubs]]

    domains = _infer_domains_from_urls(all_urls, oem)
    if verbose:
        print(f"    Provider: {provider_used or 'none'}")
        print(f"    Search results: {len(search_hits)} URLs, {len(hub_candidates)} hub candidates")
        print(f"    Inferred domains: {', '.join(domains[:6]) or '(none)'}")

    return OemDiscoveryResult(
        oem=oem,
        domains_added=domains,
        hubs_added=hub_candidates,
        search_urls_seen=len(search_hits),
        provider=provider_used,
    )


def discover_and_register_oem(
    oem: str,
    product_name: Optional[str] = None,
    max_hubs: int = 8,
    run_crawl: bool = False,
    crawl_depth: int = 4,
    verbose: bool = True,
) -> OemDiscoveryResult:
    """
    Discover hubs/domains via web search, persist to DB, optionally crawl.
    """
    result = discover_oem_hubs(oem, product_name, max_hubs=max_hubs, verbose=verbose)

    for domain in result.domains_added:
        db.add_oem_domain(oem, domain, source="discovery")

    hubs_registered = 0
    for url in result.hubs_added:
        before = db.url_exists(url)
        db.add_hub_url(oem, url, _classify_hub_url(url))
        if not before:
            hubs_registered += 1
        db.register_pending_url(oem, url, None, 0, _classify_hub_url(url))

    if verbose:
        print(f"    Registered {len(result.domains_added)} domain(s), "
              f"{hubs_registered} new hub URL(s)")

    if run_crawl and hubs_registered + len(db.get_hub_urls(oem)) > 0:
        if verbose:
            print(f"    Starting crawl for '{oem}' (depth={crawl_depth})...")
        from crawler import crawl_oem
        crawl_oem(oem, max_depth=crawl_depth, verbose=verbose)

    return result


def _static_oem_domains(oem: str) -> list[str]:
    from fuzzy_match import match_registry_key
    from seed_data import OEM_DOMAINS

    key = match_registry_key(oem, OEM_DOMAINS)
    return list(OEM_DOMAINS[key]) if key else []


def oem_readiness(oem: str) -> dict:
    """Check whether OEM has enough configuration to crawl/query."""
    static_domains = _static_oem_domains(oem)
    dynamic_domains = db.get_dynamic_oem_domains(oem)
    hubs = db.get_hub_urls(oem)
    return {
        "hub_count": len(hubs),
        "domain_count": len(set(static_domains + dynamic_domains)),
        "has_static_seed": bool(static_domains),
        "has_dynamic_domains": bool(dynamic_domains),
    }


def ensure_oem_ready(
    oem: str,
    product_name: Optional[str] = None,
    verbose: bool = True,
    auto_crawl: bool = False,
) -> bool:
    """
    If OEM lacks hubs/domains, run discovery. Returns True if OEM is ready after call.
    Controlled by OEM_AUTO_DISCOVER (default true).
    """
    flag = os.getenv("OEM_AUTO_DISCOVER", "true").lower().strip()
    if flag in ("0", "false", "no", "off"):
        return False

    try:
        min_hubs = max(1, int(os.getenv("OEM_MIN_HUBS", "1")))
    except ValueError:
        min_hubs = 1

    state = oem_readiness(oem)
    needs_discovery = (
        state["hub_count"] < min_hubs
        or state["domain_count"] == 0
    )

    if not needs_discovery:
        return False

    if verbose:
        print(f"  [i] OEM '{oem}' not fully configured "
              f"(hubs={state['hub_count']}, domains={state['domain_count']}) — auto-discovering...")

    try:
        min_depth = int(os.getenv("OEM_DISCOVER_CRAWL_DEPTH", "4"))
    except ValueError:
        min_depth = 4

    discover_and_register_oem(
        oem,
        product_name=product_name,
        run_crawl=auto_crawl,
        crawl_depth=min_depth,
        verbose=verbose,
    )
    return True


def get_merged_oem_domains(oem: str) -> list[str]:
    """Static seed_data domains + dynamically discovered domains."""
    static = _static_oem_domains(oem)
    dynamic = db.get_dynamic_oem_domains(oem)
    merged: list[str] = []
    seen: set[str] = set()
    for d in static + dynamic:
        if d not in seen:
            seen.add(d)
            merged.append(d)
    return merged

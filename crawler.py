"""
Crawler — BFS URL discovery from seed hubs (domain-locked).

Flow:
  1. Seeds: oem_hub_urls + promoted discovered_urls (get_crawl_seed_urls)
  2. Scrape page (with link extraction for BFS) -> save discovered_urls
  3. Queue child links -> register_pending_url immediately
  4. Promote successful lifecycle/product/licensing pages to oem_hub_urls
"""

import re
import time
from typing import Optional
from urllib.parse import urlparse
from datetime import datetime
from collections import deque

import db
from models import DiscoveredURL
from scraper import scrape_url, REQUEST_DELAY, is_url_fresh
from seed_data import get_oem_domains

MAX_DEPTH = 3
MAX_URLS_PER_OEM = 200
CRAWL_DELAY = REQUEST_DELAY

LIFECYCLE_PATTERNS = [
    r'/lifecycle',
    r'/end-of-life',
    r'/end-of-support',
    r'/eol',
    r'/eos',
    r'/support/policy',
    r'/support-policy',
    r'/product-lifecycle',
    r'/release-cycle',
    r'/support-dates',
    r'/supported-products',
    r'/supported-versions',
    r'/product-matrix',
    r'/version',
    r'/release',
    r'/support.*product',
    r'/product.*support',
]

LICENSING_PATTERNS = [
    r'/licensing',
    r'/license',
    r'/pricing',
    r'/how-to-buy',
    r'/editions',
    r'/subscription',
    r'/plans',
]

SKIP_PATTERNS = [
    r'/login',
    r'/signin',
    r'/signup',
    r'/register',
    r'/cart',
    r'/checkout',
    r'/blog/',
    r'/news/',
    r'/press/',
    r'/careers',
    r'/jobs',
    r'/privacy',
    r'/cookie',
    r'/terms',
    r'/legal',
    r'/contact',
    r'/feedback',
    r'/survey',
    r'\.(zip|exe|dmg|msi|iso|tar|gz|jpg|png|gif|svg|mp4|mp3|xlsx|xls|doc|docx)$',
    r'/download/',
    r'/community/.*reply',
    r'/community/.*comment',
]


def _is_same_oem_domain(url: str, oem: str) -> bool:
    domains = get_oem_domains(oem)
    if not domains:
        return False
    hostname = urlparse(url).netloc.lower()
    for domain in domains:
        if hostname == domain or hostname.endswith("." + domain):
            return True
    return False


def _classify_url(url: str) -> str:
    url_lower = url.lower()
    if url_lower.endswith(".csv") and any(
        k in url_lower for k in ("lifecycle", "eol", "eos", "product", "support")
    ):
        return "lifecycle"
    for pattern in LIFECYCLE_PATTERNS:
        if re.search(pattern, url_lower):
            return "lifecycle"
    for pattern in LICENSING_PATTERNS:
        if re.search(pattern, url_lower):
            return "licensing"
    if re.search(r'/products?/', url_lower):
        return "product_page"
    return "unknown"


def _should_skip(url: str) -> bool:
    url_lower = url.lower()
    return any(re.search(p, url_lower) for p in SKIP_PATTERNS)


def _is_worth_crawling(url: str) -> bool:
    if _should_skip(url):
        return False
    url_lower = url.lower()
    for pattern in LIFECYCLE_PATTERNS + LICENSING_PATTERNS:
        if re.search(pattern, url_lower):
            return True
    if re.search(r'/products?/', url_lower) or re.search(r'/support', url_lower):
        return True
    return True


def _should_follow_link(link: str, depth: int, max_depth: int) -> bool:
    if depth >= 1 and not _is_worth_crawling(link):
        return False
    if depth >= 2 and _classify_url(link) == "unknown":
        return False
    return True


def _maybe_promote(oem: str, url: str, url_type: str, depth: int, verbose: bool, indent: str):
    """Promote shallow, classified pages to hub seeds (not every deep link)."""
    if depth > 2 or url_type == "unknown":
        return
    if db.promote_to_hub(oem, url, url_type):
        if verbose:
            print(f"{indent}  [HUB+] Promoted to crawl seeds: {url_type}")


def crawl_oem(oem: str, max_depth: int = MAX_DEPTH, verbose: bool = True) -> int:
    """
    BFS crawl from seed + promoted hubs. Returns pages successfully scraped this session.
    """
    seeds = db.get_crawl_seed_urls(oem)
    if not seeds:
        print(f"  [X] No hub URLs registered for '{oem}'")
        return 0

    if verbose:
        seed_count = sum(1 for s in seeds if s.get("source") == "seed")
        disc_count = len(seeds) - seed_count
        print(f"\n{'=' * 60}")
        print(f"  Crawling: {oem}")
        print(f"  Seeds: {seed_count} manual hubs + {disc_count} promoted/discovered")
        print(f"  Max depth: {max_depth}  |  Cap: {MAX_URLS_PER_OEM}")
        print(f"{'=' * 60}")

    queue = deque()
    visited: set[str] = set()
    scraped_count = 0

    for seed in seeds:
        url = seed["hub_url"]
        if url not in visited:
            queue.append((url, 0, None))
            visited.add(url)

    while queue and scraped_count < MAX_URLS_PER_OEM:
        url, depth, parent_url = queue.popleft()

        if depth > max_depth:
            continue

        indent = "  " + "  " * depth if verbose else ""
        if verbose:
            print(f"{indent}{'>' if depth > 0 else '*'} [depth={depth}] {url[:80]}...")

        existing = db.get_url_info(url)
        fresh = (
            existing
            and existing.get("scrape_success")
            and existing.get("last_scraped")
            and is_url_fresh(existing["last_scraped"])
        )

        if fresh:
            if verbose:
                print(f"{indent}  [FRESH] Re-fetching for new links...")
            result = scrape_url(url, need_links=True)
            time.sleep(CRAWL_DELAY)
            if result and result.get("links"):
                _enqueue_children(
                    result, url, depth, oem, max_depth, queue, visited, verbose, indent,
                )
            continue

        result = scrape_url(url, need_links=True)
        time.sleep(CRAWL_DELAY)

        url_type = _classify_url(url)

        if not result:
            if verbose:
                print(f"{indent}  [X] Failed to scrape")
            db.save_discovered_url(
                DiscoveredURL(
                    url=url,
                    oem=oem,
                    discovered_from=parent_url,
                    depth=depth,
                    url_type=url_type,
                    last_scraped=datetime.now().isoformat(),
                    scrape_success=False,
                )
            )
            continue

        db.save_discovered_url(
            DiscoveredURL(
                url=url,
                oem=oem,
                discovered_from=parent_url,
                depth=depth,
                url_type=url_type,
                last_scraped=datetime.now().isoformat(),
                scrape_success=True,
                content_hash=result["hash"],
            )
        )
        if result.get("text"):
            db.save_url_text_snippet(url, result["text"])
        scraped_count += 1

        if depth == 0 and parent_url is None:
            db.mark_hub_crawled(url)

        _maybe_promote(oem, url, url_type, depth, verbose, indent)

        link_count = len(result.get("links") or [])
        if verbose:
            print(f"{indent}  [OK] {len(result['text'])} chars, {link_count} links, "
                  f"type={url_type}, method={result.get('method', '?')}")

        _enqueue_children(
            result, url, depth, oem, max_depth, queue, visited, verbose, indent,
        )

    if verbose:
        print(f"\n  {'-' * 50}")
        print(f"  [OK] Crawl complete for {oem}")
        print(f"    Pages scraped this session: {scraped_count}")
        print(f"    discovered_urls total: {db.get_url_count(oem)}")
        print(f"    hub_urls total: {len(db.get_hub_urls(oem))}")
        print()

    return scraped_count


def _enqueue_children(
    result: dict,
    parent_url: str,
    depth: int,
    oem: str,
    max_depth: int,
    queue: deque,
    visited: set,
    verbose: bool,
    indent: str,
):
    if depth >= max_depth:
        return

    queued = 0
    for link in result.get("links") or []:
        if not _is_same_oem_domain(link, oem):
            continue
        if link in visited:
            continue
        if not _should_follow_link(link, depth, max_depth):
            continue

        link_type = _classify_url(link)
        db.register_pending_url(oem, link, parent_url, depth + 1, link_type)
        visited.add(link)
        queue.append((link, depth + 1, parent_url))
        queued += 1

    if verbose and queued:
        print(f"{indent}  -> Queued {queued} links (saved to discovered_urls)")


def crawl_all_oems(max_depth: int = MAX_DEPTH, verbose: bool = True) -> int:
    oems = db.get_all_oems()
    if not oems:
        print("  [X] No OEMs registered. Run seed_data.py first.")
        return 0

    total = 0
    for oem in oems:
        total += crawl_oem(oem, max_depth, verbose)

    print(f"\n{'=' * 60}")
    print(f"  Total pages scraped: {total}")
    print(f"  discovered_urls: {db.get_url_count()}")
    print(f"{'=' * 60}")
    return total

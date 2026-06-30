"""
Engine — tiered query orchestrator.

Layer 0 : SQLite products cache     (instant replay of verified extractions)
Layer 1 : endoflife.date API        (structured EoL/EoS; no licensing fields)
Layer 2 : discovered_urls + scrape  (re-scrape crawled pages, rank by mentions)
Layer 3 : URL discovery + pipeline
          - search/orchestrator: cache -> crawl DB -> Exa -> DDGS fallback (cached)
          - Scrape up to 20 URLs, 3 concurrent workers (ThreadPoolExecutor)
          - stop_event cancels in-flight scrapes once enough relevant pages exist
          - Score pages; send top 5 relevant pages to Gemini (max 5)
          - Return on first extraction with confidence >= 0.55
Layer 4 : QueryResult(found=False) with reason (never hallucinate)
"""

import os
import re
import db
from urllib.parse import urlparse
import threading
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from models import ProductLifecycle, QueryResult
from sources.endoflife_api import search_endoflife
from scraper import scrape_url
from extractor import extract_from_content, CONFIDENCE_THRESHOLD
from searcher import search_product_lifecycle
from search.orchestrator import _score_url_lifecycle_relevance
from version_resolver import prepare_query
from product_matching import score_product_mentions
from product_aliases import get_aliases, get_search_name_variants
from result_quality import accept_as_found, annotate_partial


def _layer2_prescreen_limit() -> int:
    try:
        return max(5, int(os.getenv("LAYER2_MAX_PRESCREEN", "40")))
    except ValueError:
        return 40


def _layer2_max_extract() -> int:
    try:
        return max(1, int(os.getenv("LAYER2_MAX_EXTRACT", "5")))
    except ValueError:
        return 5


def _layer2_get_page_text(url: str) -> Optional[dict]:
    """Use gzip-cached crawl snippet when available; else scrape and cache."""
    snippet = db.get_url_text_snippet(url)
    if snippet and len(snippet) >= 50:
        return {"text": snippet, "hash": None}
    result = scrape_url(url)
    if result and result.get("text"):
        db.save_url_text_snippet(url, result["text"])
    return result


def _is_lifecycle_matrix_page(text: str, url: str, url_type: str) -> bool:
    """OEM support matrix pages that list many products without repeating full names."""
    if url_type not in ("lifecycle", "product_hub", "product_page"):
        url_lower = url.lower()
        if not any(
            k in url_lower
            for k in ("lifecycle", "end-of-support", "support-lifecycle", "retired", "eos", "eol")
        ):
            return False
    if not re.search(r"\d{4}", text):
        return False
    return bool(
        re.search(
            r"end of (?:support|life|sale)|eos|eol|support (?:ends|ended|policy)|retired",
            text,
            re.IGNORECASE,
        )
    )


def _layer2_page_eligible(
    text: str,
    url: str,
    url_type: str,
    product_name: str,
    oem: str,
    base_name: str,
) -> bool:
    match = score_product_mentions(text, product_name, base_name)
    if match["exact_hits"] or match["base_hits"]:
        return True
    if match["relevance_score"] > 0:
        return True
    if not _is_lifecycle_matrix_page(text, url, url_type):
        return False
    text_lower = text.lower()
    for alias in get_aliases(product_name, oem):
        if len(alias) > 3 and alias.lower() in text_lower:
            return True
    for token in re.findall(r"[a-z0-9]+", base_name.lower()):
        if len(token) > 3 and token in urlparse(url).path.lower():
            return True
    return url_type in ("lifecycle", "product_hub")


def _return_if_acceptable(
    result: ProductLifecycle,
    oem: str,
    verbose: bool,
    label: str,
) -> Optional[QueryResult]:
    if not accept_as_found(result, CONFIDENCE_THRESHOLD):
        return None
    if result.confidence < CONFIDENCE_THRESHOLD:
        result = annotate_partial(result)
        if verbose:
            print(f"  [OK] {label} (partial, confidence={result.confidence:.0%})")
    elif verbose:
        print(f"  [OK] {label} (confidence={result.confidence:.0%})")
    db.save_product(result)
    _promote_verified_source(oem, result.source_url, result.confidence)
    return QueryResult(found=True, data=result)


def _log_query_failure(product_name: str, oem: str, stats: dict) -> None:
    best = stats.get("best_confidence")
    best_str = f"{best:.3f}" if best is not None else "None"
    print(f"\n  [FAIL REPORT] {product_name} | {oem}")
    print(f"    layer2_candidates : {stats.get('layer2_candidates', 0)}")
    print(f"    layer2_eligible   : {stats.get('layer2_eligible', 0)}")
    print(f"    layer3_scraped    : {stats.get('layer3_scraped', 0)}")
    print(f"    layer3_relevant   : {stats.get('layer3_relevant', 0)}")
    print(f"    layer3_pipeline   : {stats.get('layer3_pipeline', 0)}")
    print(f"    best_confidence   : {best_str}")


def _promote_verified_source(oem: str, source_url: str, confidence: float):
    """Promote OEM crawl seeds only for confident OEM-page extractions."""
    if confidence < CONFIDENCE_THRESHOLD:
        return
    if "endoflife.date" in source_url:
        return
    from crawler import _classify_url
    db.promote_to_hub(oem, source_url, _classify_url(source_url), verified=True)


def _is_lifecycle_catalog_url(url: str) -> bool:
    """OEM-wide product/support matrices (many products, one page)."""
    path = urlparse(url).path.lower()
    return any(
        p in path
        for p in (
            "eol-matrix",
            "support-periods",
            "support-matrix",
            "supported-product-versions",
            "support-policy",
            "product-life",
            "lifecycle",
            "lifetime-support",
        )
    )


def _scrape_max_chars(url: str) -> int:
    """Matrix/catalog pages need more than the default 12k text cap."""
    if _is_lifecycle_catalog_url(url):
        try:
            return max(12000, int(os.getenv("MATRIX_SCRAPE_MAX_CHARS", "48000")))
        except ValueError:
            return 48000
    return 12000


def _score_page(text: str, url: str, product_name: str, base_name: str, version: str) -> int:
    """
    Score a scraped page by relevance to the exact product (not sibling editions).
    Uses boundary-aware matching — avoids counting WAS inside WAS ND.
    Lifecycle catalog URLs (eol-matrix, etc.) score on URL path even when the
  product row is absent from a truncated scrape.
    """
    url_bonus = _score_url_lifecycle_relevance(url)
    catalog = _is_lifecycle_catalog_url(url)

    match = score_product_mentions(text, product_name, base_name)
    text_score = match["relevance_score"]

    if match["ambiguous"]:
        text_score = max(text_score, 8)

    if text_score == 0 and not catalog and url_bonus < 20:
        return 0

    score = text_score + url_bonus

    if catalog:
        score = max(score, 25)
    elif _is_lifecycle_matrix_page(text, url, "lifecycle"):
        score = max(score, 22)

    if version and version in text:
        score += 20

    return score


# URLs matching these patterns are always irrelevant — skip before scraping
_JUNK_URL_PATTERNS = [
    "/blog", "/blogs/", "/news/", "/press-release", "/careers",
    "/about-us", "/contact", "/legal", "/privacy", "/cookie",
    "/login", "/signin", "/register", "/events/", "/webinar",
    "/investor", "/annual-report", "/salary", "/trends",
]


def _is_junk_url(url: str) -> bool:
    """Check if URL matches known irrelevant patterns."""
    url_lower = url.lower()
    return any(pattern in url_lower for pattern in _JUNK_URL_PATTERNS)


def _scrape_and_score(url: str, product_name: str, base_name: str,
                      version: str, stop_event: threading.Event) -> Optional[tuple]:
    """
    Worker function: scrape a URL and score it.
    Returns (score, url, result) or None if failed/stopped/junk.
    """
    if stop_event.is_set():
        return None

    # Skip obviously irrelevant URLs (blogs, news, careers, etc.)
    if _is_junk_url(url):
        return None

    result = scrape_url(url, max_chars=_scrape_max_chars(url))
    if not result or not result["text"] or len(result["text"]) < 50:
        return None

    if stop_event.is_set():
        return None

    score = _score_page(result["text"], url, product_name, base_name, version)
    return (score, url, result)


def query(
    product_name: str,
    oem: str,
    verbose: bool = True,
    force_refresh: bool = False,
) -> QueryResult:
    """
    Main entry point. Tries each layer in order, stops at first confident hit.
    Guarantees: every returned result has source_url populated.
    """
    product_name = product_name.strip()
    oem = oem.strip()

    from oem_discovery import ensure_oem_ready
    ensure_oem_ready(oem, product_name=product_name, verbose=verbose)

    prep = prepare_query(product_name, oem, verbose=verbose)
    version = prep.version
    base_name = prep.base_name.lower()
    search_name = prep.effective_name

    if verbose:
        print(f"\n{'-' * 60}")
        print(f"  Querying: {product_name} | {oem}" + (f" (v{version})" if version else ""))
        if prep.version_auto_resolved:
            print(f"  (latest version auto-resolved)")
        print(f"{'-' * 60}")

    # -- Layer 0: Cache --
    if force_refresh:
        db.delete_product_cache(product_name, oem)
        if verbose:
            print("  [i] Cache bypassed (--refresh)")
    cached = db.get_product(product_name, oem)
    if cached:
        if verbose:
            print(f"  [OK] Cache hit  (source: {cached.data_source}, "
                  f"confidence: {cached.confidence:.0%})")
        return QueryResult(found=True, data=cached)

    # -- Layer 1: endoflife.date --
    if verbose:
        print("  -> Layer 1: Checking endoflife.date API...")
    eol = search_endoflife(
        prep.base_name or product_name,
        oem,
        version=version,
        version_auto_resolved=prep.version_auto_resolved,
    )
    if eol:
        eol.product_name = product_name
    if eol:
        if verbose:
            print(f"  [OK] Found via endoflife.date  (confidence: {eol.confidence:.0%})")
        db.save_product(eol)
        return QueryResult(found=True, data=eol)
    if verbose:
        print("  [X] Not found in endoflife.date")

    # -- Layer 2: Discovered URLs DB --
    if verbose:
        print("  -> Layer 2: Searching discovered URLs database...")
    discovered_urls = db.get_discovered_urls(oem)

    candidate_urls = []
    if not discovered_urls:
        if verbose:
            print(f"  [X] No discovered URLs for '{oem}'. Proceeding to web search.")
    else:
        candidate_urls = [
            u for u in discovered_urls
            if u.scrape_success
            and u.url_type in (
                "lifecycle", "product_page", "product_hub", "licensing", "unknown",
            )
        ]

    if candidate_urls:
        if verbose:
            print(f"    Found {len(candidate_urls)} candidate URLs — pre-screening...")

        alias_names = get_search_name_variants(search_name, oem)
        ranked_url_list = db.rank_discovered_urls_for_product(
            oem, search_name, limit=25, extra_names=alias_names,
        )
        by_url = {u.url: u for u in candidate_urls}
        ordered: list = []
        for url in ranked_url_list:
            if url in by_url:
                ordered.append(by_url[url])
        seen_order = {u.url for u in ordered}
        for u in candidate_urls:
            if u.url not in seen_order:
                ordered.append(u)

        ranked_candidates = []
        for url_obj in ordered[: _layer2_prescreen_limit()]:
            result = _layer2_get_page_text(url_obj.url)
            if not result or not result["text"]:
                continue
            if not _layer2_page_eligible(
                result["text"], url_obj.url, url_obj.url_type,
                search_name, oem, base_name,
            ):
                continue
            match = score_product_mentions(result["text"], search_name, base_name)
            relevance_score = match["relevance_score"]
            if _is_lifecycle_matrix_page(result["text"], url_obj.url, url_obj.url_type):
                relevance_score = max(relevance_score, 10)
            ranked_candidates.append((relevance_score, url_obj, result))

        ranked_candidates.sort(key=lambda x: x[0], reverse=True)

        if not ranked_candidates:
            if verbose:
                print(f"    [X] Product not found on any crawled page.")
        else:
            if verbose:
                print(f"    {len(ranked_candidates)} pages eligible — extracting...")

            best_result: Optional[ProductLifecycle] = None
            best_confidence = 0.0

            for relevance_score, url_obj, result in ranked_candidates[: _layer2_max_extract()]:
                if verbose:
                    print(f"    -> [{relevance_score} hits] {url_obj.url[:70]}...")

                extraction = extract_from_content(
                    content=result["text"],
                    product_name=product_name,
                    oem=oem,
                    source_url=url_obj.url,
                    depth=url_obj.depth,
                    target_version=version,
                    version_auto_resolved=prep.version_auto_resolved,
                    resolved_version=version,
                )

                if extraction and extraction.confidence > best_confidence:
                    best_result = extraction
                    best_confidence = extraction.confidence
                    if verbose:
                        print(f"      [OK] confidence={extraction.confidence:.0%}")
                    if accept_as_found(extraction, CONFIDENCE_THRESHOLD):
                        break
                elif verbose:
                    print(f"      [X] Rejected")

            if best_result:
                accepted = _return_if_acceptable(
                    best_result, oem, verbose, "Best result from crawl DB",
                )
                if accepted:
                    return accepted

    # -- Layer 3: Web Search + Concurrent Pipeline --
    if verbose:
        print("  -> Layer 3: Autonomous Web Search (concurrent)...")

    web_urls = search_product_lifecycle(search_name, oem, max_results=20)
    web_count = len(web_urls) if web_urls else 0
    if web_urls:
        db.record_search_urls(oem, web_urls)

    if web_urls:
        product_version = version or ""
        # ── CONCURRENT SCRAPING (3 workers) ──
        # stop_event: workers check before/after scrape; set when we have
        # enough high-relevance pages so pending tasks exit early.
        stop_event = threading.Event()
        scraped_pages = []
        MIN_RELEVANCE_SCORE = 20
        ENOUGH_RELEVANT_PAGES = 3

        if verbose:
            print(f"    Scraping {len(web_urls)} URLs concurrently (3 workers)...")

        with ThreadPoolExecutor(max_workers=3) as executor:
            future_to_url = {
                executor.submit(
                    _scrape_and_score, url, search_name, base_name,
                    product_version, stop_event
                ): url
                for url in web_urls
            }

            for future in as_completed(future_to_url):
                if stop_event.is_set():
                    for pending in future_to_url:
                        pending.cancel()
                    break
                try:
                    result = future.result()
                    if result:
                        scraped_pages.append(result)
                        score = result[0]
                        if score >= MIN_RELEVANCE_SCORE:
                            strong = sum(
                                1 for p in scraped_pages
                                if p[0] >= MIN_RELEVANCE_SCORE
                            )
                            if strong >= ENOUGH_RELEVANT_PAGES:
                                if verbose:
                                    print(f"    [STOP] {strong} strong pages — "
                                          f"cancelling remaining scrapes")
                                stop_event.set()
                                for pending in future_to_url:
                                    pending.cancel()
                                break
                except Exception:
                    pass

        # ── RANK BY SCORE (most promising first) ──
        scraped_pages.sort(key=lambda x: x[0], reverse=True)

        relevant = [p for p in scraped_pages if p[0] > 0]
        irrelevant = [p for p in scraped_pages if p[0] == 0]

        if verbose:
            print(f"    Scraped {len(scraped_pages)} pages: "
                  f"{len(relevant)} relevant, {len(irrelevant)} no match.")

        # ── PIPELINE TO GEMINI (top scored pages, max 5) ──
        pipeline = scraped_pages[:5]

        best_web_result: Optional[ProductLifecycle] = None
        best_web_confidence = 0.0
        extracted_count = 0

        for i, (score, url, result) in enumerate(pipeline):
            if verbose:
                print(f"    [{i+1}/{len(pipeline)}] [score={score:3d}] "
                      f"{url[:55]}... -> Gemini")

            extraction = extract_from_content(
                content=result["text"],
                product_name=product_name,
                oem=oem,
                source_url=url,
                depth=0,
                target_version=version,
                version_auto_resolved=prep.version_auto_resolved,
                resolved_version=version,
            )
            extracted_count += 1

            if extraction and extraction.confidence > best_web_confidence:
                best_web_result = extraction
                best_web_confidence = extraction.confidence
                if verbose:
                    print(f"      [OK] confidence={extraction.confidence:.0%}")

                if accept_as_found(extraction, CONFIDENCE_THRESHOLD):
                    stop_event.set()
                    accepted = _return_if_acceptable(
                        best_web_result, oem, verbose,
                        f"Found after {extracted_count} extractions",
                    )
                    if accepted:
                        return accepted
            elif verbose:
                print(f"      [X] Rejected")

        if best_web_result:
            accepted = _return_if_acceptable(
                best_web_result, oem, verbose,
                f"Best web result ({extracted_count} analyzed)",
            )
            if accepted:
                return accepted

        if verbose:
            print(f"    Exhausted: {len(scraped_pages)} scraped, "
                  f"{extracted_count} sent to Gemini.")

    # -- Layer 4: Null + Reason --
    _log_query_failure(
        product_name,
        oem,
        {
            "layer2_candidates": len(candidate_urls),
            "layer2_eligible": len(ranked_candidates) if "ranked_candidates" in locals() else 0,
            "layer3_scraped": len(scraped_pages) if "scraped_pages" in locals() else 0,
            "layer3_relevant": len(relevant) if "relevant" in locals() else 0,
            "layer3_pipeline": len(pipeline) if "pipeline" in locals() else 0,
            "best_confidence": locals().get("best_web_confidence"),
        },
    )
    return QueryResult(
        found=False,
        reason=(
            f"Searched {len(candidate_urls)} crawled + {web_count} OEM web results "
            f"but could not extract confident lifecycle data for "
            f"'{product_name}' by '{oem}'."
        ),
    )


def batch_query(products: list[dict], verbose: bool = True) -> list[QueryResult]:
    """
    Query multiple products. Each dict should have 'product_name' and 'oem' keys.
    Returns list of QueryResults in the same order.
    """
    results = []
    for i, product in enumerate(products):
        if verbose:
            print(f"\n[{i + 1}/{len(products)}]", end="")
        result = query(
            product["product_name"],
            product["oem"],
            verbose=verbose,
        )
        results.append(result)
    return results

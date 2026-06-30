"""
Scraper — Council of Scrapers (fallback chain per URL).

Order in scrape_url() (except PDFs):
  1. PDF URLs     -> PyPDF2 download + text extract
  2. Crawl4AI     -> headless browser (optional; pip install crawl4ai)
  3. Jina Reader  -> https://r.jina.ai/{url} (free, no API key)
  4. Direct HTTP  -> requests + BeautifulSoup text extraction

Returns dict: {text, links, hash, method} or None if all methods fail.
Direct HTTP also extracts <a href> links (used by crawler BFS).

Freshness (crawler only):
  - is_url_fresh(last_scraped) skips re-scrape if within FRESHNESS_DAYS (15)
"""

import requests
import hashlib
import time
import asyncio
from bs4 import BeautifulSoup
from typing import Optional
from urllib.parse import urljoin, urlparse, urldefrag
from datetime import datetime, timedelta

# --- Freshness Configuration ---
FRESHNESS_DAYS = 15  # URLs scraped within this window are considered fresh


def is_url_fresh(last_scraped: Optional[str], freshness_days: int = FRESHNESS_DAYS) -> bool:
    """
    Check if a URL was scraped recently enough to be considered fresh.
    Returns True if we can skip re-scraping it.
    """
    if not last_scraped:
        return False  # Never scraped -- needs scraping
    try:
        scraped_time = datetime.fromisoformat(last_scraped)
        cutoff = datetime.now() - timedelta(days=freshness_days)
        return scraped_time > cutoff
    except (ValueError, TypeError):
        return False


# --- Method 1: Direct Requests (fast, free) ---

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Sec-CH-UA": '"Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Windows"',
    "Cache-Control": "max-age=0",
}

REQUEST_DELAY = 1.5

_session = None

def _get_session() -> requests.Session:
    """Lazy-init a persistent session with browser-like defaults."""
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update(HEADERS)
    return _session


def _fetch_direct(url: str, timeout: int = 20) -> Optional[str]:
    """Method 1: Direct HTTP request with browser-like headers."""
    session = _get_session()
    try:
        r = session.get(url, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        return r.text
    except requests.exceptions.HTTPError as e:
        print(f"    [Scraper:Direct] HTTP {e.response.status_code} for {url}")
        return None
    except requests.exceptions.RequestException as e:
        print(f"    [Scraper:Direct] Failed: {type(e).__name__}")
        return None


# --- Method 2: Jina AI Reader (free, handles JS + anti-bot) ---

JINA_READER_PREFIX = "https://r.jina.ai/"

def _fetch_jina(url: str, timeout: int = 30, max_chars: int = 12000) -> Optional[dict]:
    """
    Method 2: Jina AI Reader.
    Free API that renders JS, bypasses anti-bot, returns clean markdown.
    No API key required.
    """
    jina_url = f"{JINA_READER_PREFIX}{url}"
    try:
        r = requests.get(
            jina_url,
            headers={"Accept": "text/plain"},
            timeout=timeout,
        )
        if r.status_code != 200:
            print(f"    [Scraper:Jina] HTTP {r.status_code} for {url}")
            return None

        text = r.text.strip()
        if not text or len(text) < 50:
            print(f"    [Scraper:Jina] Empty or too short for {url}")
            return None

        return {
            "text": text[:max_chars],
            "links": [],
            "hash": hashlib.md5(text[:max_chars].encode("utf-8")).hexdigest(),
            "method": "jina",
        }
    except requests.exceptions.RequestException as e:
        print(f"    [Scraper:Jina] Failed: {type(e).__name__}")
        return None


# --- Method 3: Crawl4AI (headless browser, optional) ---

def _fetch_crawl4ai(url: str, max_chars: int = 12000) -> Optional[dict]:
    """
    Method 3: Crawl4AI -- headless browser scraping.
    Handles JavaScript-rendered pages and advanced anti-bot.
    Only used if crawl4ai is installed (pip install crawl4ai).
    """
    try:
        from crawl4ai import AsyncWebCrawler
    except ImportError:
        return None  # crawl4ai not installed -- skip silently

    import os
    # Fix Windows cp1252 encoding crash with crawl4ai's rich logger
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    async def _crawl():
        try:
            async with AsyncWebCrawler(verbose=False) as crawler:
                result = await crawler.arun(url=url)
                if result and result.markdown:
                    text = result.markdown.strip()[:max_chars]
                    return {
                        "text": text,
                        "links": [],
                        "hash": hashlib.md5(text.encode("utf-8")).hexdigest(),
                        "method": "crawl4ai",
                    }
        except Exception as e:
            print(f"    [Scraper:Crawl4AI] Failed: {type(e).__name__}")
        return None

    try:
        return asyncio.run(_crawl())
    except RuntimeError:
        # If there's already an event loop running
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_crawl())
        finally:
            loop.close()


# --- HTML Processing (for Method 1) ---

def extract_text(html: str, max_chars: int = 12000) -> str:
    """
    Extract clean text from HTML, stripping navigation, footers, scripts, etc.
    Prefers semantic containers (<main>, <article>, #content).
    """
    soup = BeautifulSoup(html, "html.parser")

    # Remove noise elements
    for tag in soup(["script", "style", "nav", "footer", "header",
                     "aside", "form", "iframe", "noscript", "svg",
                     "button", "input", "select"]):
        tag.decompose()

    # Try semantic content containers first
    main = (
        soup.find("main")
        or soup.find("article")
        or soup.find(id="content")
        or soup.find(id="main-content")
        or soup.find(class_="content")
        or soup.find(class_="main-content")
        or soup.find(role="main")
        or soup.body
    )

    if not main:
        return ""

    text = main.get_text(separator="\n", strip=True)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    cleaned = "\n".join(lines)

    return cleaned[:max_chars]


def extract_links(html: str, base_url: str) -> list[str]:
    """
    Extract all <a href> links from HTML, resolved to absolute URLs.
    Deduplicates and strips fragments.
    """
    soup = BeautifulSoup(html, "html.parser")
    links = set()

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].strip()
        if href.startswith(("javascript:", "mailto:", "tel:", "#", "data:")):
            continue
        absolute = urljoin(base_url, href)
        absolute, _ = urldefrag(absolute)
        if absolute.startswith(("http://", "https://")):
            links.add(absolute)

    return list(links)


def content_hash(text: str) -> str:
    """Generate a hash of page content for change detection."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


# --- Method 4: PDF Text Extraction ---

def _path_extension(url: str) -> str:
    return urlparse(url).path.lower().rsplit(".", 1)[-1] if "." in urlparse(url).path else ""


def _is_pdf_url(url: str) -> bool:
    return _path_extension(url) == "pdf"


def _is_csv_url(url: str) -> bool:
    return _path_extension(url) == "csv"


# Playwright/Crawl4AI cannot render file downloads — fetch via HTTP or skip
_BROWSER_SKIP_EXTENSIONS = frozenset({
    "zip", "gz", "tar", "bz2", "7z", "rar",
    "exe", "msi", "dmg", "iso", "deb", "rpm",
    "jpg", "jpeg", "png", "gif", "svg", "webp", "ico", "mp4", "mp3", "wav",
    "xlsx", "xls", "doc", "docx", "ppt", "pptx",
})


def _is_browser_skip_url(url: str) -> bool:
    """Non-HTML assets that must not go through Crawl4AI (avoids 'Download is starting')."""
    return _path_extension(url) in _BROWSER_SKIP_EXTENSIONS


def _fetch_pdf(url: str, max_chars: int = 12000) -> Optional[dict]:
    """
    Download and extract text from a PDF file.
    Uses PyPDF2 for lightweight, pure-Python PDF parsing.
    """
    try:
        from PyPDF2 import PdfReader
        import io
    except ImportError:
        print(f"    [Scraper:PDF] PyPDF2 not installed. Run: pip install PyPDF2")
        return None

    try:
        r = _get_session().get(url, timeout=30, allow_redirects=True)
        if r.status_code != 200:
            print(f"    [Scraper:PDF] HTTP {r.status_code} for {url}")
            return None

        # Check content type
        content_type = r.headers.get("Content-Type", "")
        if "pdf" not in content_type and not _is_pdf_url(url):
            return None

        reader = PdfReader(io.BytesIO(r.content))
        text_parts = []
        for page in reader.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)

        text = "\n".join(text_parts).strip()
        if not text or len(text) < 50:
            print(f"    [Scraper:PDF] No readable text in PDF")
            return None

        text = text[:max_chars]
        print(f"    [Scraper:PDF] Success! Extracted {len(text)} chars from {len(reader.pages)} pages")
        return {
            "text": text,
            "links": [],
            "hash": content_hash(text),
            "method": "pdf",
        }
    except Exception as e:
        print(f"    [Scraper:PDF] Failed: {type(e).__name__}: {e}")
        return None


def _fetch_csv(url: str, max_chars: int = 12000) -> Optional[dict]:
    """
    Download CSV as plain text (IBM product lifecycle lists, etc.).
    Avoids Crawl4AI/Playwright 'Download is starting' errors.
    """
    try:
        r = _get_session().get(url, timeout=45, allow_redirects=True)
        if r.status_code != 200:
            print(f"    [Scraper:CSV] HTTP {r.status_code} for {url[:80]}")
            return None

        content_type = (r.headers.get("Content-Type") or "").lower()
        if "html" in content_type:
            return None

        raw = r.content
        for encoding in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                text = ""
        else:
            print(f"    [Scraper:CSV] Could not decode response")
            return None

        text = text.strip()
        if len(text) < 30:
            print(f"    [Scraper:CSV] Empty or too small")
            return None

        text = text[:max_chars]
        print(f"    [Scraper:CSV] Success! Got {len(text)} chars")
        return {
            "text": text,
            "links": [],
            "hash": content_hash(text),
            "method": "csv",
        }
    except requests.exceptions.RequestException as e:
        print(f"    [Scraper:CSV] Failed: {type(e).__name__}")
        return None


# --- Main Entry Point: Council of Scrapers ---

def _supplement_links(url: str, result: dict) -> dict:
    """
    Jina/Crawl4AI return text but no hrefs — fetch HTML once for BFS link discovery.
    """
    if result.get("links"):
        return result
    html = _fetch_direct(url)
    if html:
        links = extract_links(html, url)
        if links:
            result = {**result, "links": links}
            print(f"    [Scraper] +{len(links)} links via direct HTML pass")
    return result


def scrape_url(url: str, max_chars: int = 12000, need_links: bool = False) -> Optional[dict]:
    """
    Council of Scrapers: PDF -> CSV -> Crawl4AI -> Jina -> direct HTTP.

    need_links=True (crawler): always attach <a href> list for recursive BFS.
    """
    from url_safety import is_safe_url

    if not is_safe_url(url):
        print(f"    [Scraper] Blocked unsafe URL: {url[:80]}")
        return None

    if _is_pdf_url(url):
        return _fetch_pdf(url, max_chars=max_chars)

    if _is_csv_url(url):
        return _fetch_csv(url, max_chars=max_chars)

    if _is_browser_skip_url(url):
        print(f"    [Scraper] Skipping non-HTML download (.{_path_extension(url)}): {url[:70]}...")
        return None

    # --- Method 1: Crawl4AI (best — headless browser) ---
    c4ai_result = _fetch_crawl4ai(url, max_chars=max_chars)
    if c4ai_result:
        print(f"    [Scraper:Crawl4AI] Success! Got {len(c4ai_result['text'])} chars")
        if need_links:
            c4ai_result = _supplement_links(url, c4ai_result)
        return c4ai_result

    # --- Method 2: Jina AI Reader ---
    jina_result = _fetch_jina(url, max_chars=max_chars)
    if jina_result:
        print(f"    [Scraper:Jina] Success! Got {len(jina_result['text'])} chars")
        if need_links:
            jina_result = _supplement_links(url, jina_result)
        return jina_result

    # --- Method 3: Direct requests (fallback) ---
    html = _fetch_direct(url)
    if html:
        text = extract_text(html, max_chars)
        if text and len(text) > 50:
            links = extract_links(html, url)
            return {
                "text": text,
                "links": links,
                "hash": content_hash(text),
                "method": "direct",
            }

    # All methods failed
    return None


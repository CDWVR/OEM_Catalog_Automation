"""
Search providers — each returns a list of dicts with 'href' (and optional 'title').

Primary (recommended): Exa — set EXA_API_KEY, SEARCH_PROVIDER=auto|exa
Fallback:            DuckDuckGo (ddgs) — free, rate-limited
Legacy opt-in:       searxng, brave

Canonical Exa docs: https://docs.exa.ai/reference/search-api-guide-for-coding-agents
"""

import os
import time
from typing import List, Dict, Optional, Protocol
from urllib.parse import urlparse

import requests

try:
    from ddgs import DDGS
except ImportError:
    DDGS = None

try:
    from exa_py import Exa
except ImportError:
    Exa = None


class SearchProvider(Protocol):
    name: str

    def search(self, query: str, max_results: int = 5) -> List[Dict]:
        ...


class ExaSearchProvider:
    """
    Exa neural web search (https://docs.exa.ai).
    Uses type=deep with highlights by default for lifecycle URL discovery.
    """


    name = "exa"

    def __init__(self, api_key: str, search_type: str = "deep"):
        if Exa is None:
            raise ImportError("exa-py not installed. Run: pip install exa-py")
        self._exa = Exa(api_key=api_key)
        self.search_type = search_type

    def _parse_results(self, response, max_results: int) -> List[Dict]:
        out: List[Dict] = []
        results = getattr(response, "results", None) or []
        for item in results:
            url = getattr(item, "url", None)
            title = getattr(item, "title", "") or ""
            if url and url.startswith("http"):
                out.append({"href": url, "title": title})
            if len(out) >= max_results:
                break
        return out

    def search(
        self,
        query: str,
        max_results: int = 5,
        include_domains: Optional[list[str]] = None,
    ) -> List[Dict]:
        """Single Exa search. Pass include_domains to restrict to OEM sites."""
        try:
            kwargs: dict = {
                "type": self.search_type,
                "num_results": min(max_results, 100),
                "contents": {"highlights": True},
            }
            if include_domains:
                kwargs["include_domains"] = include_domains[:1200]
                kwargs["system_prompt"] = (
                    "Prefer official vendor documentation, support lifecycle pages, "
                    "and product support matrices. Avoid forums and news unless no "
                    "official source exists."
                )

            response = self._exa.search(query, **kwargs)
            return self._parse_results(response, max_results)
        except Exception as e:
            err = str(e)
            if "401" in err or "Invalid" in err or "api key" in err.lower():
                print("  [X] Exa: invalid or missing API key")
            elif "429" in err:
                print("  [X] Exa: rate limit exceeded")
            else:
                print(f"  [X] Exa search failed: {type(e).__name__} — {err[:120]}")
            return []

    def search_lifecycle(
        self,
        product_name: str,
        oem: str,
        oem_domains: list[str],
        max_results: int = 10,
    ) -> List[Dict]:
        """
        One or two Exa calls tuned for lifecycle data (fewer API calls than DDGS loop).
        """
        domains = oem_domains[:30] if oem_domains else None
        query = (
            f'Official {oem} support lifecycle documentation for "{product_name}": '
            f"end of support, end of extended support, end of life, support policy, "
            f"supported versions, product support matrix"
        )
        results = self.search(query, max_results=max_results, include_domains=domains)
        if len(results) >= max(3, max_results // 2):
            return results

        # Broader query without domain lock if OEM-restricted search was thin
        if domains:
            open_query = (
                f'"{oem}" "{product_name}" official end of support end of life '
                f"lifecycle support policy documentation"
            )
            extra = self.search(open_query, max_results=max_results - len(results))
            seen = {r["href"] for r in results}
            for r in extra:
                if r["href"] not in seen:
                    results.append(r)
                    seen.add(r["href"])
        return results[:max_results]


class SearXNGProvider:
    """Optional free meta-search (SEARXNG_INSTANCE)."""

    name = "searxng"

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def search(self, query: str, max_results: int = 5) -> List[Dict]:
        try:
            r = requests.get(
                f"{self.base_url}/search",
                params={"q": query, "format": "json", "categories": "general"},
                headers={"User-Agent": "lifecycle-poc/2.0"},
                timeout=25,
            )
            if r.status_code != 200:
                print(f"  [X] SearXNG HTTP {r.status_code}")
                return []
            data = r.json()
            out = []
            for item in data.get("results", []):
                url = item.get("url")
                if url:
                    out.append({"href": url, "title": item.get("title", "")})
                if len(out) >= max_results:
                    break
            return out
        except requests.RequestException as e:
            print(f"  [X] SearXNG failed: {type(e).__name__}")
            return []


class BraveSearchProvider:
    """Legacy paid Brave API — SEARCH_PROVIDER=brave only."""

    name = "brave"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def search(self, query: str, max_results: int = 5) -> List[Dict]:
        try:
            r = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": max_results},
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": self.api_key,
                },
                timeout=20,
            )
            if r.status_code == 429:
                print("  [!] Brave Search rate limit")
                return []
            r.raise_for_status()
            out = []
            for item in r.json().get("web", {}).get("results", []):
                url = item.get("url")
                if url:
                    out.append({"href": url, "title": item.get("title", "")})
            return out
        except requests.RequestException as e:
            print(f"  [X] Brave Search failed: {type(e).__name__}")
            return []


class DuckDuckGoProvider:
    """Free fallback — rate-limited."""

    name = "ddgs"

    def search(self, query: str, max_results: int = 5, retries: int = 3) -> List[Dict]:
        if DDGS is None:
            print("  [X] 'ddgs' not installed. Run: pip install ddgs")
            return []

        for attempt in range(retries):
            try:
                return list(DDGS().text(query, max_results=max_results))
            except Exception as e:
                err_msg = str(e)
                if "Ratelimit" in err_msg or "202 Accept" in err_msg or type(e).__name__ == "DDGSException":
                    time.sleep(2 ** attempt)
                else:
                    print(f"  [X] DDGS failed: {type(e).__name__}")
                    return []
        print("  [X] DDGS failed after retries.")
        return []


def get_exa_provider() -> Optional[ExaSearchProvider]:
    api_key = os.getenv("EXA_API_KEY", "").strip()
    if not api_key:
        return None
    search_type = os.getenv("EXA_SEARCH_TYPE", "deep").strip() or "deep"
    try:
        return ExaSearchProvider(api_key=api_key, search_type=search_type)
    except ImportError:
        print("  [X] exa-py not installed. Run: pip install exa-py")
        return None


def get_provider(name: str) -> SearchProvider | None:
    """Resolve provider by name from environment."""
    key = name.lower().strip()
    if key == "exa":
        return get_exa_provider()
    if key == "searxng":
        instance = os.getenv("SEARXNG_INSTANCE", "").strip()
        if instance:
            return SearXNGProvider(instance)
        return None
    if key == "brave":
        api_key = os.getenv("BRAVE_SEARCH_API_KEY", "").strip()
        if api_key:
            return BraveSearchProvider(api_key)
        return None
    if key == "ddgs":
        return DuckDuckGoProvider()
    return None


def is_oem_url(url: str, oem_domains: list[str]) -> bool:
    if not oem_domains:
        return False
    hostname = urlparse(url).netloc.lower()
    for domain in oem_domains:
        if hostname == domain or hostname.endswith("." + domain):
            return True
    return False

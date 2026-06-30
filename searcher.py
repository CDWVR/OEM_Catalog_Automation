"""
searcher.py — Layer 3 URL discovery entry point.

Delegates to search/orchestrator.py (cache -> crawl DB -> Exa -> DDGS fallback).
"""

from search.orchestrator import search_product_lifecycle

__all__ = ["search_product_lifecycle"]

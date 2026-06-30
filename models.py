"""
Pydantic models shared across engine, db, crawler, and extractor.

ProductLifecycle — a single verified result (source_url required)
QueryResult        — found flag + data or reason when not found
DiscoveredURL      — crawler provenance for discovered_urls table
"""

from pydantic import BaseModel
from typing import Optional


class ProductLifecycle(BaseModel):
    """Core result model — every field is optional because we prefer null over hallucination."""
    product_name: str
    oem: str
    end_of_support: Optional[str] = None
    end_of_extended_support: Optional[str] = None
    end_of_life: Optional[str] = None
    licensing_model: Optional[str] = None
    chargeability_type: Optional[str] = None
    source_url: str                         # REQUIRED — no source = no result
    confidence: float = 0.0
    data_source: str = ""                   # e.g. "endoflife.date", "oem_crawl", "cache"
    last_verified: Optional[str] = None
    # Version resolution
    resolved_version: Optional[str] = None
    version_auto_resolved: bool = False
    # Relative / estimated lifecycle (see date_precision)
    release_date: Optional[str] = None
    support_policy_relative: Optional[str] = None
    estimated_end_of_support: Optional[str] = None
    date_precision: Optional[str] = None    # exact | estimated | relative_only
    lifecycle_notes: Optional[str] = None


class QueryResult(BaseModel):
    """Wrapper — always tells the caller *why* something wasn't found."""
    found: bool
    data: Optional[ProductLifecycle] = None
    reason: Optional[str] = None            # Actionable reason when found=False


class DiscoveredURL(BaseModel):
    """A URL found by the recursive crawler, with full provenance."""
    url: str
    oem: str
    discovered_from: Optional[str] = None   # Parent URL
    depth: int = 0                          # Hops from seed hub
    url_type: str = "unknown"               # lifecycle | licensing | product_page | unknown
    last_scraped: Optional[str] = None
    scrape_success: Optional[bool] = None
    content_hash: Optional[str] = None      # For change detection on re-crawl
    trust_score: float = 1.0                # Legacy column; always 1.0 (ranking uses content/url_type)
    verified: bool = False                  # Promoted hub / confirmed lifecycle page

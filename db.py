"""
Database layer — SQLite persistence.

Tables:
  1. oem_hub_urls      — Manually curated seed URLs (crawl entry points)
  2. discovered_urls   — URLs found by the recursive crawler (with provenance)
  3. products          — Cached lifecycle extraction results
  4. search_cache      — Cached web-search URL lists (reduces API/scraper load)
"""

import os
import sqlite3
import hashlib
import json
import gzip
import re
from typing import Optional
from datetime import datetime, timedelta
from urllib.parse import urlparse
from models import ProductLifecycle, DiscoveredURL

DB_PATH = "lifecycle_v2.db"


def _conn():
    return sqlite3.connect(DB_PATH)


# ─── Schema ─────────────────────────────────────────────────────────

def init_db():
    """Create all tables if they don't exist."""
    conn = _conn()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS oem_hub_urls (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            oem          TEXT NOT NULL,
            hub_url      TEXT NOT NULL UNIQUE,
            url_type     TEXT DEFAULT 'lifecycle',
            last_crawled TEXT,
            active       BOOLEAN DEFAULT 1
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS discovered_urls (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            url             TEXT NOT NULL UNIQUE,
            oem             TEXT NOT NULL,
            discovered_from TEXT,
            depth           INTEGER DEFAULT 0,
            url_type        TEXT DEFAULT 'unknown',
            last_scraped    TEXT,
            scrape_success  BOOLEAN,
            content_hash    TEXT,
            trust_score     REAL DEFAULT 1.0
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            product_name           TEXT NOT NULL,
            oem                    TEXT NOT NULL,
            end_of_support         TEXT,
            end_of_extended_support TEXT,
            end_of_life            TEXT,
            licensing_model        TEXT,
            chargeability_type     TEXT,
            source_url             TEXT NOT NULL,
            confidence             REAL,
            data_source            TEXT,
            last_verified          TEXT,
            UNIQUE(product_name COLLATE NOCASE, oem COLLATE NOCASE)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS search_cache (
            cache_key    TEXT PRIMARY KEY,
            urls_json    TEXT NOT NULL,
            provider     TEXT,
            created_at   TEXT NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS oem_dynamic_domains (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            oem        TEXT NOT NULL,
            domain     TEXT NOT NULL,
            source     TEXT DEFAULT 'discovery',
            created_at TEXT NOT NULL,
            UNIQUE(oem, domain)
        )
    """)

    _migrate_products_schema(conn)
    _migrate_discovered_schema(conn)
    conn.commit()
    conn.close()


def _migrate_discovered_schema(conn):
    c = conn.cursor()
    c.execute("PRAGMA table_info(discovered_urls)")
    existing = {row[1] for row in c.fetchall()}
    if "verified" not in existing:
        c.execute("ALTER TABLE discovered_urls ADD COLUMN verified INTEGER DEFAULT 0")
    if "text_snippet_gzip" not in existing:
        c.execute("ALTER TABLE discovered_urls ADD COLUMN text_snippet_gzip BLOB")
    conn.commit()


_SNIPPET_MAX_CHARS = 12000


def _snippet_max_chars() -> int:
    try:
        return max(2000, int(os.getenv("CRAWL_SNIPPET_MAX_CHARS", str(_SNIPPET_MAX_CHARS))))
    except ValueError:
        return _SNIPPET_MAX_CHARS


def compress_text_snippet(text: str) -> Optional[bytes]:
    if not text:
        return None
    payload = text[: _snippet_max_chars()].encode("utf-8")
    return gzip.compress(payload, compresslevel=6)


def decompress_text_snippet(blob: Optional[bytes]) -> Optional[str]:
    if not blob:
        return None
    try:
        return gzip.decompress(blob).decode("utf-8", errors="replace")
    except (OSError, gzip.BadGzipFile):
        return None


def get_url_text_snippet(url: str) -> Optional[str]:
    conn = _conn()
    c = conn.cursor()
    c.execute("SELECT text_snippet_gzip FROM discovered_urls WHERE url = ?", (url,))
    row = c.fetchone()
    conn.close()
    return decompress_text_snippet(row[0] if row else None)


def save_url_text_snippet(url: str, text: str):
    blob = compress_text_snippet(text)
    if not blob:
        return
    conn = _conn()
    conn.execute(
        "UPDATE discovered_urls SET text_snippet_gzip = ? WHERE url = ?",
        (blob, url),
    )
    conn.commit()
    conn.close()


_PRODUCT_EXTRA_COLUMNS = [
    ("resolved_version", "TEXT"),
    ("version_auto_resolved", "INTEGER DEFAULT 0"),
    ("release_date", "TEXT"),
    ("support_policy_relative", "TEXT"),
    ("estimated_end_of_support", "TEXT"),
    ("date_precision", "TEXT"),
    ("lifecycle_notes", "TEXT"),
]


def _migrate_products_schema(conn):
    """Add new product columns to existing databases."""
    c = conn.cursor()
    c.execute("PRAGMA table_info(products)")
    existing = {row[1] for row in c.fetchall()}
    for name, col_type in _PRODUCT_EXTRA_COLUMNS:
        if name not in existing:
            c.execute(f"ALTER TABLE products ADD COLUMN {name} {col_type}")
    conn.commit()


_PRODUCT_SELECT = """
    product_name, oem, end_of_support, end_of_extended_support,
    end_of_life, licensing_model, chargeability_type,
    source_url, confidence, data_source, last_verified,
    resolved_version, version_auto_resolved, release_date,
    support_policy_relative, estimated_end_of_support,
    date_precision, lifecycle_notes
"""


def _row_to_product(row: tuple) -> ProductLifecycle:
    keys = [k.strip() for k in _PRODUCT_SELECT.replace("\n", " ").split(",")]
    data = dict(zip(keys, row))
    if data.get("version_auto_resolved") is not None:
        data["version_auto_resolved"] = bool(data["version_auto_resolved"])
    return ProductLifecycle(**data)


# ─── Hub URLs ────────────────────────────────────────────────────────

def add_hub_url(oem: str, hub_url: str, url_type: str = "lifecycle"):
    """Insert a seed hub URL (idempotent)."""
    conn = _conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO oem_hub_urls (oem, hub_url, url_type) VALUES (?, ?, ?)",
            (oem, hub_url, url_type)
        )
        conn.commit()
    finally:
        conn.close()


def get_hub_urls(oem: str) -> list[dict]:
    """Get all active hub URLs for an OEM."""
    conn = _conn()
    c = conn.cursor()
    c.execute(
        "SELECT hub_url, url_type FROM oem_hub_urls WHERE oem = ? COLLATE NOCASE AND active = 1",
        (oem,)
    )
    rows = c.fetchall()
    conn.close()
    return [{"hub_url": r[0], "url_type": r[1]} for r in rows]


def get_all_oems() -> list[str]:
    """All OEMs with hubs and/or dynamically registered domains."""
    conn = _conn()
    c = conn.cursor()
    c.execute("""
        SELECT DISTINCT oem FROM oem_hub_urls WHERE active = 1
        UNION
        SELECT DISTINCT oem FROM oem_dynamic_domains
    """)
    rows = c.fetchall()
    conn.close()
    return sorted({r[0] for r in rows}, key=str.lower)


def add_oem_domain(oem: str, domain: str, source: str = "discovery"):
    """Register a trusted domain for an OEM (from discovery or manual)."""
    domain = domain.lower().strip().lstrip(".")
    if not domain:
        return
    conn = _conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO oem_dynamic_domains (oem, domain, source, created_at) "
            "VALUES (?, ?, ?, ?)",
            (oem.strip(), domain, source, datetime.now().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def get_dynamic_oem_domains(oem: str) -> list[str]:
    conn = _conn()
    c = conn.cursor()
    c.execute(
        "SELECT domain FROM oem_dynamic_domains WHERE oem = ? COLLATE NOCASE ORDER BY id",
        (oem,),
    )
    rows = [r[0] for r in c.fetchall()]
    conn.close()
    return rows


def list_discovered_oems() -> list[dict]:
    """OEMs registered only via discovery (may also have hubs)."""
    conn = _conn()
    c = conn.cursor()
    c.execute("""
        SELECT oem, COUNT(DISTINCT domain) AS domains,
               (SELECT COUNT(*) FROM oem_hub_urls h
                WHERE h.oem = oem_dynamic_domains.oem COLLATE NOCASE AND h.active = 1) AS hubs
        FROM oem_dynamic_domains
        GROUP BY oem
        ORDER BY oem
    """)
    rows = [{"oem": r[0], "domains": r[1], "hubs": r[2]} for r in c.fetchall()]
    conn.close()
    return rows


def mark_hub_crawled(hub_url: str):
    """Update last_crawled timestamp for a hub URL."""
    conn = _conn()
    conn.execute(
        "UPDATE oem_hub_urls SET last_crawled = ? WHERE hub_url = ?",
        (datetime.now().isoformat(), hub_url)
    )
    conn.commit()
    conn.close()


# URL types worth promoting into oem_hub_urls for the next crawl wave
PROMOTABLE_URL_TYPES = frozenset({
    "lifecycle", "product_hub", "product_page", "licensing",
})


def promote_to_hub(oem: str, url: str, url_type: str, verified: bool = False) -> bool:
    """
    Promote a scraped/verified URL to oem_hub_urls so future crawls start from it.
    """
    if url_type not in PROMOTABLE_URL_TYPES:
        return False
    hub_type = "product_hub" if url_type == "product_page" else url_type
    add_hub_url(oem, url, hub_type)
    if verified:
        mark_url_verified(url)
    return True


def get_crawl_seed_urls(oem: str, max_promoted: int = 50) -> list[dict]:
    """
    BFS entry points: manual hubs + verified/promoted discovered URLs only.
    Avoids re-seeding every scraped page (prevents crawl explosion).
    """
    seen: set[str] = set()
    seeds: list[dict] = []
    for hub in get_hub_urls(oem):
        u = hub["hub_url"]
        if u not in seen:
            seen.add(u)
            seeds.append({"hub_url": u, "url_type": hub["url_type"], "source": "seed"})
    conn = _conn()
    c = conn.cursor()
    c.execute(
        """SELECT d.url, d.url_type FROM discovered_urls d
           WHERE d.oem = ? COLLATE NOCASE
             AND d.scrape_success = 1
             AND (
               d.verified = 1
               OR EXISTS (
                 SELECT 1 FROM oem_hub_urls h
                 WHERE h.hub_url = d.url AND h.active = 1
               )
             )
           ORDER BY d.verified DESC, d.last_scraped DESC
           LIMIT ?""",
        (oem, max_promoted),
    )
    for url, url_type in c.fetchall():
        if url not in seen:
            seen.add(url)
            seeds.append({"hub_url": url, "url_type": url_type, "source": "promoted"})
    conn.close()
    return seeds


# ─── Discovered URLs ────────────────────────────────────────────────

def save_discovered_url(url_obj: DiscoveredURL, verified: bool = False):
    """Insert or update a discovered URL."""
    conn = _conn()
    try:
        conn.execute("""
            INSERT INTO discovered_urls
                (url, oem, discovered_from, depth, url_type, last_scraped,
                 scrape_success, content_hash, trust_score, verified)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                oem = excluded.oem,
                discovered_from = COALESCE(excluded.discovered_from, discovered_from),
                depth = MIN(discovered_urls.depth, excluded.depth),
                url_type = CASE
                    WHEN excluded.url_type != 'unknown' THEN excluded.url_type
                    ELSE discovered_urls.url_type
                END,
                last_scraped = COALESCE(excluded.last_scraped, discovered_urls.last_scraped),
                scrape_success = CASE
                    WHEN excluded.last_scraped IS NOT NULL THEN excluded.scrape_success
                    ELSE discovered_urls.scrape_success
                END,
                content_hash = COALESCE(excluded.content_hash, discovered_urls.content_hash),
                verified = MAX(discovered_urls.verified, excluded.verified)
        """, (
            url_obj.url, url_obj.oem, url_obj.discovered_from,
            url_obj.depth, url_obj.url_type, url_obj.last_scraped,
            url_obj.scrape_success, url_obj.content_hash, url_obj.trust_score,
            1 if verified else 0,
        ))
        conn.commit()
    finally:
        conn.close()


def register_pending_url(
    oem: str,
    url: str,
    parent_url: Optional[str],
    depth: int,
    url_type: str = "unknown",
):
    """Store a URL as soon as it is queued (before scrape completes)."""
    if url_exists(url):
        return
    save_discovered_url(
        DiscoveredURL(
            url=url,
            oem=oem,
            discovered_from=parent_url,
            depth=depth,
            url_type=url_type,
            last_scraped=None,
            scrape_success=False,
        )
    )


def mark_url_verified(url: str):
    conn = _conn()
    conn.execute("UPDATE discovered_urls SET verified = 1 WHERE url = ?", (url,))
    conn.commit()
    conn.close()


_URL_TYPE_ORDER = """
    CASE url_type
        WHEN 'lifecycle' THEN 0
        WHEN 'product_hub' THEN 1
        WHEN 'product_page' THEN 2
        WHEN 'licensing' THEN 3
        ELSE 4
    END
"""


def get_discovered_urls(oem: str, url_type: Optional[str] = None) -> list[DiscoveredURL]:
    """Get discovered URLs for an OEM, ordered by url_type then recency (not depth)."""
    conn = _conn()
    c = conn.cursor()

    order = f"ORDER BY {_URL_TYPE_ORDER}, scrape_success DESC, last_scraped DESC"

    cols = (
        "url, oem, discovered_from, depth, url_type, last_scraped, "
        "scrape_success, content_hash, trust_score, verified"
    )
    if url_type:
        c.execute(
            f"SELECT {cols} FROM discovered_urls "
            f"WHERE oem = ? COLLATE NOCASE AND url_type = ? {order}",
            (oem, url_type),
        )
    else:
        c.execute(
            f"SELECT {cols} FROM discovered_urls WHERE oem = ? COLLATE NOCASE {order}",
            (oem,),
        )

    rows = c.fetchall()
    conn.close()

    keys = [
        "url", "oem", "discovered_from", "depth", "url_type",
        "last_scraped", "scrape_success", "content_hash", "trust_score", "verified",
    ]
    out = []
    for r in rows:
        data = dict(zip(keys, r))
        data["verified"] = bool(data.get("verified"))
        out.append(DiscoveredURL(**data))
    return out


def url_exists(url: str) -> bool:
    """Check if a URL has already been discovered."""
    conn = _conn()
    c = conn.cursor()
    c.execute("SELECT 1 FROM discovered_urls WHERE url = ?", (url,))
    exists = c.fetchone() is not None
    conn.close()
    return exists


def get_url_info(url: str) -> Optional[dict]:
    """Get last_scraped and scrape_success for a URL (for freshness checks)."""
    conn = _conn()
    c = conn.cursor()
    c.execute(
        "SELECT last_scraped, scrape_success FROM discovered_urls WHERE url = ?",
        (url,)
    )
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    return {"last_scraped": row[0], "scrape_success": bool(row[1])}


def get_url_count(oem: Optional[str] = None) -> int:
    """Count discovered URLs, optionally filtered by OEM."""
    conn = _conn()
    c = conn.cursor()
    if oem:
        c.execute("SELECT COUNT(*) FROM discovered_urls WHERE oem = ? COLLATE NOCASE", (oem,))
    else:
        c.execute("SELECT COUNT(*) FROM discovered_urls")
    count = c.fetchone()[0]
    conn.close()
    return count


# ─── Products (Cache) ───────────────────────────────────────────────

def _product_cache_ttl_days() -> int:
    try:
        return int(os.getenv("PRODUCT_CACHE_TTL_DAYS", "30"))
    except ValueError:
        return 30


def _min_cache_confidence() -> float:
    try:
        return float(os.getenv("PRODUCT_CACHE_MIN_CONFIDENCE", "0.55"))
    except ValueError:
        return 0.55


def get_product(
    product_name: str,
    oem: str,
    *,
    respect_ttl: bool = True,
) -> Optional[ProductLifecycle]:
    """Look up cached product (TTL + confidence floor)."""
    conn = _conn()
    c = conn.cursor()
    c.execute(
        f"""SELECT {_PRODUCT_SELECT}
           FROM products
           WHERE product_name = ? COLLATE NOCASE AND oem = ? COLLATE NOCASE""",
        (product_name, oem),
    )
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    product = _row_to_product(row)
    if product.confidence < _min_cache_confidence():
        return None
    if respect_ttl and product.last_verified:
        try:
            if datetime.now() - datetime.fromisoformat(product.last_verified) > timedelta(
                days=_product_cache_ttl_days()
            ):
                return None
        except (ValueError, TypeError):
            pass
    return product


_ALL_DATA_TABLES = (
    "products",
    "discovered_urls",
    "oem_hub_urls",
    "search_cache",
    "oem_dynamic_domains",
)


def _table_row_counts(conn) -> dict[str, int]:
    c = conn.cursor()
    counts = {}
    for table in _ALL_DATA_TABLES:
        c.execute(f"SELECT COUNT(*) FROM {table}")
        counts[table] = c.fetchone()[0]
    return counts


def clear_all_data() -> dict[str, int]:
    """Delete all rows from every data table. Returns previous row counts."""
    conn = _conn()
    before = _table_row_counts(conn)
    for table in _ALL_DATA_TABLES:
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.close()
    return before


def clear_oem_data(oem: str) -> dict[str, int]:
    """Remove all data for one OEM (crawled URLs, cache, hubs, domains)."""
    conn = _conn()
    c = conn.cursor()
    before = {}
    c.execute("SELECT COUNT(*) FROM products WHERE oem = ? COLLATE NOCASE", (oem,))
    before["products"] = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM discovered_urls WHERE oem = ? COLLATE NOCASE", (oem,))
    before["discovered_urls"] = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM oem_hub_urls WHERE oem = ? COLLATE NOCASE", (oem,))
    before["oem_hub_urls"] = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM oem_dynamic_domains WHERE oem = ? COLLATE NOCASE", (oem,))
    before["oem_dynamic_domains"] = c.fetchone()[0]
    # search_cache keys are hashed — not OEM-filterable; left intact unless full reset

    c.execute("DELETE FROM products WHERE oem = ? COLLATE NOCASE", (oem,))
    c.execute("DELETE FROM discovered_urls WHERE oem = ? COLLATE NOCASE", (oem,))
    c.execute("DELETE FROM oem_hub_urls WHERE oem = ? COLLATE NOCASE", (oem,))
    c.execute("DELETE FROM oem_dynamic_domains WHERE oem = ? COLLATE NOCASE", (oem,))
    conn.commit()
    conn.close()
    return before


def reset_database(delete_file: bool = False) -> dict[str, int]:
    """
    Wipe all persisted data. If delete_file=True, remove lifecycle_v2.db and recreate schema.
    """
    if delete_file:
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
        init_db()
        return {t: 0 for t in _ALL_DATA_TABLES}
    return clear_all_data()


def delete_product_cache(product_name: str, oem: str) -> bool:
    conn = _conn()
    c = conn.cursor()
    c.execute(
        "DELETE FROM products WHERE product_name = ? COLLATE NOCASE AND oem = ? COLLATE NOCASE",
        (product_name, oem),
    )
    deleted = c.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def record_search_urls(oem: str, urls: list[str], source: str = "web_search"):
    """Persist Layer 3 URLs for future crawl/search reuse."""
    for url in urls:
        if not url or not url.startswith("http"):
            continue
        if url_exists(url):
            continue
        save_discovered_url(
            DiscoveredURL(
                url=url,
                oem=oem,
                discovered_from=source,
                depth=0,
                url_type="unknown",
                scrape_success=False,
            )
        )


def save_product(product: ProductLifecycle):
    """Cache a product result (insert or update)."""
    conn = _conn()
    try:
        conn.execute(f"""
            INSERT OR REPLACE INTO products
                ({_PRODUCT_SELECT.replace(chr(10), ' ')})
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            product.product_name, product.oem,
            product.end_of_support, product.end_of_extended_support,
            product.end_of_life, product.licensing_model,
            product.chargeability_type, product.source_url,
            product.confidence, product.data_source,
            product.last_verified or datetime.now().isoformat(),
            product.resolved_version,
            1 if product.version_auto_resolved else 0,
            product.release_date,
            product.support_policy_relative,
            product.estimated_end_of_support,
            product.date_precision,
            product.lifecycle_notes,
        ))
        conn.commit()
    finally:
        conn.close()


def get_all_products(limit: Optional[int] = None) -> list[ProductLifecycle]:
    """Return cached products, newest first."""
    conn = _conn()
    c = conn.cursor()
    sql = f"SELECT {_PRODUCT_SELECT} FROM products ORDER BY id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    c.execute(sql)
    rows = c.fetchall()
    conn.close()
    return [_row_to_product(r) for r in rows]


def deactivate_hub_url(hub_url: str):
    """Mark a seed hub URL inactive (e.g. after 404 verification)."""
    conn = _conn()
    conn.execute("UPDATE oem_hub_urls SET active = 0 WHERE hub_url = ?", (hub_url,))
    conn.commit()
    conn.close()


# ─── Search cache ───────────────────────────────────────────────────

def _search_cache_key(oem: str, product_name: str, provider: str) -> str:
    raw = f"{oem.lower().strip()}|{product_name.lower().strip()}|{provider}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_search_cache(oem: str, product_name: str, provider: str,
                     ttl_hours: int = 168) -> Optional[list[str]]:
    """Return cached URL list if still fresh."""
    conn = _conn()
    c = conn.cursor()
    key = _search_cache_key(oem, product_name, provider)
    c.execute(
        "SELECT urls_json, created_at FROM search_cache WHERE cache_key = ?",
        (key,),
    )
    row = c.fetchone()
    conn.close()
    if not row:
        return None
    try:
        created = datetime.fromisoformat(row[1])
        if datetime.now() - created > timedelta(hours=ttl_hours):
            return None
        return json.loads(row[0])
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def save_search_cache(oem: str, product_name: str, provider: str, urls: list[str]):
    """Persist URL list from a search provider."""
    conn = _conn()
    key = _search_cache_key(oem, product_name, provider)
    conn.execute(
        "INSERT OR REPLACE INTO search_cache (cache_key, urls_json, provider, created_at) "
        "VALUES (?, ?, ?, ?)",
        (key, json.dumps(urls), provider, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def rank_discovered_urls_for_product(
    oem: str,
    product_name: str,
    limit: int = 20,
    extra_names: Optional[list[str]] = None,
) -> list[str]:
    """
    Rank crawled URLs for a product without calling a search engine.
    Prefers lifecycle/product URL types and path tokens from the product name.
    """
    urls = get_discovered_urls(oem)
    if not urls:
        return []

    tokens: set[str] = set()
    for name in [product_name] + (extra_names or []):
        for t in re.findall(r"[a-z0-9]+", name.lower()):
            if len(t) > 2:
                tokens.add(t)
    tokens = list(tokens)
    type_bonus = {
        "lifecycle": 25,
        "product_hub": 20,
        "product_page": 15,
        "licensing": 10,
        "unknown": 0,
    }

    scored: list[tuple[int, str]] = []
    for item in urls:
        if not item.scrape_success:
            continue
        path = urlparse(item.url).path.lower()
        score = type_bonus.get(item.url_type, 0)
        for token in tokens:
            if token in path:
                score += 8
            if token.replace(".", "-") in path or token.replace(".", "_") in path:
                score += 4
        if score > 0 or item.url_type in ("lifecycle", "product_hub", "product_page"):
            scored.append((score, item.url))

    scored.sort(key=lambda x: x[0], reverse=True)
    seen = set()
    result = []
    for _score, url in scored:
        if url not in seen:
            seen.add(url)
            result.append(url)
        if len(result) >= limit:
            break
    return result

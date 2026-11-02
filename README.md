# Product Lifecycle Query Engine v2

POC for catalog enrichment: given **product name + OEM**, return end-of-support, end-of-life, licensing, and chargeability with **source URL and confidence**. Design rule: **null beats hallucination**.

## Setup

```bash
cd OEM_Catalog_Automation
python -m venv venv
source venv/bin/activate          # Windows: .\venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env              # Windows: copy .env.example .env
# Edit .env — add GEMINI_API_KEY and EXA_API_KEY (see Layer 3 search below).
```

Run any command once to init DB and seed hubs: `python main.py stats`

Run the offline tests: `python -m unittest discover -s tests`

## Commands

| Command | Description |
|---------|-------------|
| `python main.py` | Interactive queries |
| `python main.py crawl --oem "Microsoft"` | Crawl one OEM |
| `python main.py crawl --all` | Crawl all seeded OEMs |
| `python main.py query -p "Windows Server 2019" -o "Microsoft"` | Single lookup |
| `python main.py batch -i products.xlsx -o results.xlsx` | Excel batch |
| `python main.py stats` | DB statistics |
| `python check_results.py --limit 20` | Inspect cache |

## Query pipeline (engine.py)

| Layer | Source | Notes |
|-------|--------|-------|
| 0 | SQLite `products` | Instant cache |
| 1 | endoflife.date API | ~300+ products; no licensing |
| 2 | `discovered_urls` | Re-scrape crawled pages; rank by product mentions |
| 3 | Search + scrape + Gemini | See below |
| 4 | Not found | `QueryResult` with reason |

## Layer 3: URL discovery (Exa)

**Primary web search:** [Exa API](https://docs.exa.ai/reference/search-api-guide-for-coding-agents) (`exa-py`), configured for **`type=deep`** with **`contents.highlights`**. One Exa call replaces many DuckDuckGo `site:` queries.

| Step | Source | Notes |
|------|--------|--------|
| 1 | `discovered_urls` (crawl) | Free — re-ranked every query; URLs Layer 2 already tried are skipped |
| 2 | SQLite `search_cache` | Web-search URLs reused for 7 days (default) |
| 3 | **Exa** | `EXA_API_KEY` — OEM `include_domains` filtering |
| 4 | DuckDuckGo / `ddgs` | Fallback only if Exa/crawl did not return enough URLs |

`.env`:

```env
EXA_API_KEY=your_exa_api_key_here
EXA_SEARCH_TYPE=deep
EXA_NUM_RESULTS=10
SEARCH_PROVIDER=auto
SEARCH_CACHE_TTL_HOURS=168
GEMINI_API_KEY=...
```

Get an Exa key: https://dashboard.exa.ai/api-keys

**Recommended workflow:**

1. `pip install exa-py` (or `pip install -r requirements.txt`)
2. Add `EXA_API_KEY` to `.env`
3. `python main.py crawl --all` (fills crawl DB, reduces Exa calls)
4. `python main.py batch -i products.xlsx -o results.xlsx`

Use `EXA_SEARCH_TYPE=auto` for faster/cheaper searches; `deep` for harder lifecycle lookups (4–15s per search).

## Scraper order (scraper.py)

Per URL: **PDF → Crawl4AI → Jina Reader → direct HTTP** (not the old direct-first order).

## Layer 3: early cancellation

After **3 pages** score ≥ 20 (product/version mention), `stop_event` is set and pending scrape
futures are cancelled. Workers check `stop_event.is_set()` at the start and end of
`_scrape_and_score()`.

## Extraction safety (null beats hallucination)

Scraped pages are untrusted input to Gemini. The extractor therefore:

- delimits page text in the prompt and tells the model never to follow instructions inside it;
- requests JSON output (`response_mime_type=application/json`);
- **drops any extracted date whose year does not appear on the page**, and lowers confidence
  when the year appears but not the full date;
- recomputes estimated end-of-support itself instead of trusting a model-computed estimate;
- treats community/forum hosts (`community.*`, `forums.*`, `techcommunity.*`, ...) as off-domain
  for confidence scoring, even when they sit under a trusted OEM domain.

## Crawl-first strategy (point 7 — robust end-to-end)

For a **reliable catalog pipeline** without depending on search APIs:

```
1. seed_data.py  → hub URLs in oem_hub_urls
2. crawl --all   → fills discovered_urls (domain-locked BFS)
3. batch/query   → Layer 2 uses crawl DB; Layer 3 adds search only if needed
4. cache         → Layer 0 avoids repeat Gemini calls
```

Expand coverage by adding rows to `SEED_HUBS` and `OEM_DOMAINS` in `seed_data.py`, then re-run crawl.

## Security

- Keep real keys only in `.env` (gitignored). `.env.example` uses placeholders.
- If a key was ever committed, rotate it in Google Cloud.
- **SSRF:** `url_safety.is_safe_url()` resolves hostnames and rejects any non-public address
  (loopback, RFC1918, link-local/metadata, CGNAT, numeric tricks such as `http://2130706433/`).
  Direct HTTP fetches go through `safe_get()`, which re-validates every redirect hop and caps
  response size (`SCRAPE_MAX_BYTES`, default 20 MB). DNS rebinding is narrowed, not eliminated:
  don't run the crawler on a host with access to sensitive internal services.
- **Spreadsheet output:** values starting with `= + - @` are written as text (formula / DDE
  injection protection) — see `output_safety.py`.
- **Source trust:** OEM auto-discovery only trusts domains named after the vendor (`acme.com`,
  not `acme-eol.co.uk` or Wikipedia). Third-party search hits are never stored as crawl seeds.
- **Third parties:** URLs are sent to Jina Reader (`r.jina.ai`); product/OEM names go to Exa,
  DuckDuckGo and Gemini. Review this if your catalog is confidential.

## Project layout

```
main.py          CLI
engine.py        5-layer query orchestrator
db.py            SQLite (products, discovered_urls, search_cache)
crawler.py       BFS crawler
scraper.py       Council of scrapers
extractor.py     Gemini + confidence gate + date grounding
search/          URL discovery orchestrator + providers
seed_data.py     Hub URLs + OEM domains
sources/         endoflife.date client
url_safety.py    SSRF checks + safe_get (redirect re-validation, size cap)
domain_utils.py  Shared hostname/domain trust helpers
output_safety.py Spreadsheet formula-injection protection
check_results.py Inspect products cache
tests/           Offline unit tests
```

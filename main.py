"""
main.py — CLI for the Product Lifecycle Query Engine v2.

Run from the project folder. On Windows, if `python` is not on PATH, use:
  .\\venv\\Scripts\\python.exe main.py <command> ...

Requires .env: GEMINI_API_KEY (Layers 2–3), EXA_API_KEY (recommended for search/discovery).


================================================================================
1) INTERACTIVE MODE — type product + OEM at prompts (no subcommand)
================================================================================

  python main.py
  .\\venv\\Scripts\\python.exe main.py

  At prompts enter product name and OEM; type quit / exit / q to leave.
  Uses the same 5-layer query engine as `query` below (cache → endoflife.date
  → crawled DB → web search + Gemini → not found).


================================================================================
2) QUERY — single product lifecycle lookup (main use case)
================================================================================

  python main.py query -p "<product>" -o "<oem>"

  Examples — seeded OEM, version in name:
    python main.py query -p "Windows Server 2019" -o "Microsoft"
    python main.py query -p "Control-M 9.0" -o "BMC Software"
    python main.py query -p "RHEL 8" -o "Red Hat"

  Examples — product without version (engine may resolve latest):
    python main.py query -p "Windows Server" -o "Microsoft"
    python main.py query -p "VMware vSphere" -o "VMware"

  Examples — edition / disambiguation (exact name matters):
    python main.py query -p "WebSphere Application Server 9.0" -o "IBM"
    python main.py query -p "Alias Design 2021" -o "Autodesk"

  Bypass SQLite product cache and force a fresh run:
    python main.py query -p "Windows Server 2019" -o "Microsoft" --refresh

  Unknown OEM (no seed hubs): auto-discovery runs first if OEM_AUTO_DISCOVER=true:
    python main.py query -p "Photoshop 2024" -o "Adobe"


================================================================================
3) BATCH — Excel in, Excel out (many products)
================================================================================

  python main.py batch -i <input.xlsx> -o <output.xlsx>

  Default output filename if -o omitted:
    python main.py batch -i products.xlsx

  Custom output path:
    python main.py batch -i products.xlsx -o results_may2026.xlsx

  Input must have columns like Publisher/OEM/Vendor + Product Name/Product
  (names are detected flexibly). Rows with Status=Found are skipped on re-run.
  Tip: run `crawl` or `discover --crawl` for OEMs first, then batch.


================================================================================
4) CRAWL — BFS from hub URLs into discovered_urls (feeds Layer 2)
================================================================================

  One OEM (auto-discovers hubs if missing):
    python main.py crawl --oem "Microsoft"
    python main.py crawl --oem "Autodesk"
    python main.py crawl --oem "BMC Software"

  Deeper crawl (more pages, slower) — or set CRAWL_MAX_DEPTH in .env:
    python main.py crawl --oem "Microsoft" --depth 5

  Shallow crawl (faster smoke test):
    python main.py crawl --oem "Oracle" --depth 2

  All OEMs that have hubs in the database:
    python main.py crawl --all

  All OEMs, shallow:
    python main.py crawl --all --depth 2


================================================================================
5) DISCOVER — find OEM lifecycle hubs/domains via web (not seed-limited)
================================================================================

  Register hubs + domains only (no crawl):
    python main.py discover -o "Adobe"
    python main.py discover -o "ServiceNow"

  Discovery with optional product context (narrows search):
    python main.py discover -o "Adobe" -p "Creative Cloud"

  Discover then BFS crawl immediately:
    python main.py discover -o "Adobe" --crawl
    python main.py discover -o "Adobe" --crawl --depth 5


================================================================================
6) STATS — database summary (hubs, discovered URLs, cache, dynamic domains)
================================================================================

  python main.py stats


================================================================================
7) RESET-DB — clear crawled data / cache (start fresh)
================================================================================

  Clear ALL tables (keeps schema; re-applies seed hubs after):
    python main.py reset-db --yes

  Clear everything and delete the DB file (full wipe + recreate):
    python main.py reset-db --yes --delete-file

  Clear without re-loading seed_data.py hubs:
    python main.py reset-db --yes --no-seed

  Clear one OEM only (e.g. bad IBM/dev.to crawl):
    python main.py reset-db --yes --oem "IBM"

  Manual alternative: delete lifecycle_v2.db in the project folder, then run any command.


================================================================================
Typical workflows
================================================================================

  New OEM end-to-end:
    python main.py discover -o "Adobe" --crawl
    python main.py query -p "Photoshop 2024" -o "Adobe"

  Known OEM, improve Layer 2 coverage then query:
    python main.py crawl --oem "Autodesk" --depth 4
    python main.py query -p "Alias Design 2021" -o "Autodesk"

  Large catalog from Excel:
    python main.py crawl --all
    python main.py batch -i products.xlsx -o results.xlsx
"""

import sys
import os
import argparse

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

import db
from engine import query, batch_query
from crawler import crawl_oem, crawl_all_oems
from seed_data import seed_database
from models import QueryResult


def _default_crawl_depth() -> int:
    """Default BFS depth from CRAWL_MAX_DEPTH (.env), clamped 1–20."""
    try:
        return max(1, min(20, int(os.getenv("CRAWL_MAX_DEPTH", "4"))))
    except ValueError:
        return 4


# --- Display Helpers ---

def print_result(result: QueryResult):
    """Pretty-print a query result."""
    if not result.found:
        print(f"\n  [NOT FOUND]")
        print(f"      Reason: {result.reason}")
        return

    d = result.data
    W = 62

    print(f"\n  [FOUND]  [{d.data_source}  |  {d.confidence:.0%} confidence]")
    print(f"  {'-' * W}")
    print(f"  {'Product':<22}: {d.product_name}")
    print(f"  {'OEM':<22}: {d.oem}")
    if d.resolved_version:
        auto = " (auto)" if d.version_auto_resolved else ""
        print(f"  {'Resolved version':<22}: {d.resolved_version}{auto}")
    print(f"  {'-' * W}")
    eos_display = d.end_of_support or (
        f"~{d.estimated_end_of_support} (estimated)" if d.estimated_end_of_support else "--"
    )
    if d.date_precision == "relative_only" and d.support_policy_relative:
        eos_display = f"[policy] {d.support_policy_relative}"
    print(f"  {'End of Support':<22}: {eos_display}")
    print(f"  {'End of Extended Supp.':<22}: {d.end_of_extended_support or '--'}")
    print(f"  {'End of Life':<22}: {d.end_of_life or '--'}")
    if d.release_date:
        print(f"  {'Release date':<22}: {d.release_date}")
    if d.support_policy_relative and d.date_precision != "relative_only":
        print(f"  {'Support policy':<22}: {d.support_policy_relative}")
    if d.date_precision:
        print(f"  {'Date precision':<22}: {d.date_precision}")
    print(f"  {'Licensing Model':<22}: {d.licensing_model or '--'}")
    print(f"  {'Chargeability':<22}: {d.chargeability_type or '--'}")
    if d.lifecycle_notes:
        print(f"  {'Notes':<22}: {d.lifecycle_notes[:80]}")
    print(f"  {'-' * W}")
    print(f"  {'Source':<22}: {d.source_url}")
    print(f"  {'Last Verified':<22}: {d.last_verified}")
    print()


def print_stats():
    """Show database statistics."""
    import sqlite3
    conn = sqlite3.connect(db.DB_PATH)
    c = conn.cursor()

    print(f"\n{'=' * 60}")
    print(f"  Database Statistics")
    print(f"{'=' * 60}")

    # Hub URLs
    c.execute("SELECT oem, COUNT(*) FROM oem_hub_urls WHERE active=1 GROUP BY oem ORDER BY oem")
    hubs = c.fetchall()
    print(f"\n  Hub URLs ({sum(r[1] for r in hubs)} total):")
    for oem, count in hubs:
        print(f"    {oem:<25}: {count} hubs")

    # Discovered URLs
    c.execute("""
        SELECT oem, COUNT(*), SUM(CASE WHEN scrape_success=1 THEN 1 ELSE 0 END),
               COUNT(DISTINCT url_type)
        FROM discovered_urls GROUP BY oem ORDER BY oem
    """)
    discovered = c.fetchall()
    if discovered:
        print(f"\n  Discovered URLs ({sum(r[1] for r in discovered)} total):")
        for oem, total, success, types in discovered:
            print(f"    {oem:<25}: {total} URLs ({success or 0} scraped, {types} types)")
    else:
        print(f"\n  Discovered URLs: 0 (run crawl first)")

    # URL types breakdown
    c.execute("SELECT url_type, COUNT(*) FROM discovered_urls GROUP BY url_type ORDER BY COUNT(*) DESC")
    types = c.fetchall()
    if types:
        print(f"\n  URL Types:")
        for url_type, count in types:
            print(f"    {url_type:<25}: {count}")

    # Cached products
    c.execute("SELECT COUNT(*) FROM products")
    prod_count = c.fetchone()[0]
    print(f"\n  Cached Products: {prod_count}")

    if prod_count > 0:
        c.execute("SELECT data_source, COUNT(*), AVG(confidence) FROM products GROUP BY data_source")
        sources = c.fetchall()
        for source, count, avg_conf in sources:
            print(f"    {source:<25}: {count} products (avg confidence: {avg_conf:.0%})")

    c.execute("SELECT oem, domain, source FROM oem_dynamic_domains ORDER BY oem, domain")
    dyn = c.fetchall()
    if dyn:
        print(f"\n  Discovered OEM domains ({len(dyn)} total):")
        current = None
        for oem, domain, source in dyn:
            if oem != current:
                print(f"    {oem}:")
                current = oem
            print(f"      {domain}  ({source})")

    conn.close()
    print()


def discover_mode(oem: str, product: str = "", crawl: bool = False, depth: int = 4):
    """Discover and register lifecycle hubs for an OEM not limited to seed_data."""
    from oem_discovery import discover_and_register_oem, oem_readiness

    before = oem_readiness(oem)
    print(f"\n  Before: hubs={before['hub_count']}, domains={before['domain_count']}")

    discover_and_register_oem(
        oem,
        product_name=product or None,
        run_crawl=crawl,
        crawl_depth=depth,
        verbose=True,
    )

    after = oem_readiness(oem)
    print(f"  After:  hubs={after['hub_count']}, domains={after['domain_count']}")
    if after["hub_count"]:
        print("\n  Hub URLs:")
        for h in db.get_hub_urls(oem):
            print(f"    [{h['url_type']}] {h['hub_url']}")
    print()


# --- CLI Modes ---

def interactive_mode():
    """Interactive query mode."""
    print(f"\n{'=' * 60}")
    print(f"   Product Lifecycle Query Engine v2")
    print(f"   Accuracy-first: null > hallucination")
    print(f"{'=' * 60}")
    print(f"   Type 'quit' to exit\n")

    while True:
        try:
            product = input("  Product name : ").strip()
            if product.lower() in ("quit", "exit", "q"):
                break
            oem = input("  OEM name     : ").strip()
            if not product or not oem:
                print("  Both fields required.\n")
                continue
            result = query(product, oem)
            print_result(result)
        except KeyboardInterrupt:
            print("\n  Exiting.")
            break


def batch_mode(input_file: str, output_file: str):
    """Process an Excel file of products."""
    try:
        import pandas as pd
    except ImportError:
        print("  [X] pandas is required for batch mode: pip install pandas openpyxl")
        return

    if not os.path.exists(input_file):
        print(f"  [X] Input file not found: {input_file}")
        return

    df = pd.read_excel(input_file)
    print(f"\n  Loaded {len(df)} products from {input_file}")

    # Detect column names (flexible)
    pub_col = next((c for c in df.columns if c.lower() in ("publisher", "oem", "vendor")), None)
    prod_col = next((c for c in df.columns if c.lower() in ("product name", "product", "name")), None)

    if not pub_col or not prod_col:
        print(f"  [X] Could not find Publisher/OEM and Product Name columns.")
        print(f"    Columns found: {list(df.columns)}")
        return

    # Prepare output columns
    out_cols = [
        "End of Support", "End of Extended Support", "End of Life",
        "Estimated EoS", "Support Policy", "Release Date", "Date Precision",
        "Resolved Version", "Licensing Model", "Chargeability", "Source URL",
        "Confidence", "Data Source", "Status",
    ]
    for col in out_cols:
        if col not in df.columns:
            df[col] = ""

    for index, row in df.iterrows():
        oem = str(row[pub_col]).strip()
        product = str(row[prod_col]).strip()

        if not product or product.lower() == "nan":
            continue

        # Skip already processed
        current = str(row.get("Status", "")).strip()
        if current == "Found":
            continue

        print(f"\n[{index + 1}/{len(df)}]", end="")
        result = query(product, oem)

        if result.found:
            d = result.data
            eos = d.end_of_support or ""
            if not eos and d.estimated_end_of_support:
                eos = f"~{d.estimated_end_of_support} (est.)"
            elif not eos and d.support_policy_relative:
                eos = f"[policy] {d.support_policy_relative}"
            df.at[index, "End of Support"] = eos
            df.at[index, "End of Extended Support"] = d.end_of_extended_support or ""
            df.at[index, "End of Life"] = d.end_of_life or ""
            df.at[index, "Estimated EoS"] = d.estimated_end_of_support or ""
            df.at[index, "Support Policy"] = d.support_policy_relative or ""
            df.at[index, "Release Date"] = d.release_date or ""
            df.at[index, "Date Precision"] = d.date_precision or ""
            df.at[index, "Resolved Version"] = (
                (d.resolved_version or "") + (" (auto)" if d.version_auto_resolved else "")
            )
            df.at[index, "Licensing Model"] = d.licensing_model or ""
            df.at[index, "Chargeability"] = d.chargeability_type or ""
            df.at[index, "Source URL"] = d.source_url
            df.at[index, "Confidence"] = f"{d.confidence:.0%}"
            df.at[index, "Data Source"] = d.data_source
            df.at[index, "Status"] = "Found"
        else:
            df.at[index, "Status"] = f"Not Found: {result.reason[:100]}" if result.reason else "Not Found"

        # Save incrementally
        df.to_excel(output_file, index=False)

    print(f"\n\n  [OK] Results saved to {output_file}")
    found = len(df[df["Status"] == "Found"])
    print(f"    Found: {found}/{len(df)} ({found/len(df)*100:.0f}%)")


# --- Main ---

def main():
    parser = argparse.ArgumentParser(
        description="Product Lifecycle Query Engine v2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                                         Interactive mode
  python main.py crawl --oem "Microsoft"                 Crawl one OEM
  python main.py crawl --all                             Crawl all OEMs
  python main.py crawl --all --depth 2                   Shallow crawl
  python main.py query -p "Windows Server 2019" -o "Microsoft"
  python main.py batch -i products.xlsx -o results.xlsx
  python main.py stats                                   Database stats
  python main.py discover -o "Adobe" --crawl             Discover OEM + crawl
  python main.py reset-db --yes                          Clear entire database
        """,
    )

    subparsers = parser.add_subparsers(dest="command")

    # Crawl command
    crawl_parser = subparsers.add_parser("crawl", help="Crawl OEM websites to discover URLs")
    crawl_parser.add_argument("--oem", help="OEM name to crawl")
    crawl_parser.add_argument("--all", action="store_true", help="Crawl all registered OEMs")
    crawl_parser.add_argument(
        "--depth", type=int, default=None,
        help=f"Max link hops from hub seeds (default: CRAWL_MAX_DEPTH env or {_default_crawl_depth()})",
    )

    # Query command
    query_parser = subparsers.add_parser("query", help="Query lifecycle data for a product")
    query_parser.add_argument("-p", "--product", required=True, help="Product name")
    query_parser.add_argument("-o", "--oem", required=True, help="OEM/vendor name")
    query_parser.add_argument(
        "--refresh", action="store_true", help="Bypass product cache and re-query",
    )

    # Batch command
    batch_parser = subparsers.add_parser("batch", help="Process an Excel file")
    batch_parser.add_argument("-i", "--input", required=True, help="Input Excel file")
    batch_parser.add_argument("-o", "--output", default="results.xlsx", help="Output Excel file")

    # Stats command
    subparsers.add_parser("stats", help="Show database statistics")

    # Discover OEM command (web search — not limited to seed hubs)
    disc_parser = subparsers.add_parser(
        "discover", help="Discover OEM lifecycle hubs and domains via web search",
    )
    disc_parser.add_argument("-o", "--oem", required=True, help="OEM/vendor name")
    disc_parser.add_argument("-p", "--product", default="", help="Optional product for context")
    disc_parser.add_argument("--crawl", action="store_true", help="Run BFS crawl after discovery")
    disc_parser.add_argument(
        "--depth", type=int, default=None,
        help="Crawl depth when using --crawl (default: OEM_DISCOVER_CRAWL_DEPTH or CRAWL_MAX_DEPTH)",
    )

    reset_parser = subparsers.add_parser(
        "reset-db", help="Clear database tables (requires --yes)",
    )
    reset_parser.add_argument(
        "--yes", action="store_true",
        help="Confirm destructive reset (required)",
    )
    reset_parser.add_argument(
        "--oem", default="",
        help="Clear only this OEM (discovered URLs, hubs, domains, product cache)",
    )
    reset_parser.add_argument(
        "--delete-file", action="store_true",
        help="Delete lifecycle_v2.db and recreate empty schema",
    )
    reset_parser.add_argument(
        "--no-seed", action="store_true",
        help="Do not re-run seed_data after reset",
    )

    args = parser.parse_args()

    db.init_db()

    if args.command == "reset-db":
        if not args.yes:
            print("  [X] Destructive command — re-run with --yes to confirm.")
            print("      Examples:")
            print('        python main.py reset-db --yes')
            print('        python main.py reset-db --yes --oem "IBM"')
            print('        python main.py reset-db --yes --delete-file')
            return
        if args.oem:
            removed = db.clear_oem_data(args.oem)
            print(f"\n  [OK] Cleared data for OEM '{args.oem}':")
            for table, count in removed.items():
                print(f"    {table}: {count} rows removed")
        else:
            if args.delete_file:
                removed = db.reset_database(delete_file=True)
                print(f"\n  [OK] Deleted {db.DB_PATH} and recreated empty schema.")
            else:
                removed = db.clear_all_data()
                print(f"\n  [OK] Cleared all tables in {db.DB_PATH}:")
                for table, count in removed.items():
                    print(f"    {table}: {count} rows removed")
        if not args.no_seed and not args.oem:
            seed_database()
            print("  [OK] Re-seeded hub URLs from seed_data.py")
        elif not args.no_seed and args.oem:
            print("  [i] Per-OEM reset — seed_data not re-run (other OEMs unchanged).")
        print()
        return

    seed_database()

    if args.command == "discover":
        depth = args.depth if args.depth is not None else _default_crawl_depth()
        discover_mode(args.oem, product=args.product, crawl=args.crawl, depth=depth)

    elif args.command == "crawl":
        depth = args.depth if args.depth is not None else _default_crawl_depth()
        if args.oem:
            from oem_discovery import ensure_oem_ready
            ensure_oem_ready(args.oem, verbose=True, auto_crawl=False)
        if args.all:
            crawl_all_oems(max_depth=depth)
        elif args.oem:
            crawl_oem(args.oem, max_depth=depth)
        else:
            print("  [X] Specify --oem <name> or --all")

    elif args.command == "query":
        result = query(args.product, args.oem, force_refresh=args.refresh)
        print_result(result)

    elif args.command == "batch":
        batch_mode(args.input, args.output)

    elif args.command == "stats":
        print_stats()

    else:
        interactive_mode()


if __name__ == "__main__":
    main()

"""
check_results.py — inspect cached product rows in SQLite.

Usage:
    .\\venv\\Scripts\\python.exe check_results.py
    .\\venv\\Scripts\\python.exe check_results.py --limit 20
"""

import argparse
import db

db.init_db()

parser = argparse.ArgumentParser(description="Show cached lifecycle products")
parser.add_argument("--limit", type=int, default=10, help="Number of rows to show")
args = parser.parse_args()

products = db.get_all_products(limit=args.limit)

print("--- CACHED PRODUCTS ---")
if not products:
    print("  (empty — run a query or batch first)")
else:
    for p in products:
        print(f"{p.product_name} ({p.oem})")
        print(f"  Confidence : {p.confidence:.0%}")
        print(f"  Data source: {p.data_source}")
        print(f"  EoS        : {p.end_of_support or '--'}")
        print(f"  EoL        : {p.end_of_life or '--'}")
        print(f"  Source URL : {p.source_url[:80]}...")
        print("-" * 20)

print(f"\nTotal cached: {len(db.get_all_products())}")

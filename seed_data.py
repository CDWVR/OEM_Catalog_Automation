"""
Seed data — OEM hub URLs and trusted domain registry.

SEED_HUBS: crawl entry points (BFS starts here). URLs were verified via HTTP;
corrupted placeholders (404) were removed. Some OEM sites return 403 to simple
HTTP clients — the scraper council (Crawl4AI / Jina) may still retrieve them.

OEM_DOMAINS: used by crawler domain-lock, search OEM filtering, and extractor
confidence scoring. Keys are normalized OEM names (lowercase).
"""

import db

# ─── Seed Hub URLs ──────────────────────────────────────────────────
# Each entry: (oem, url, url_type)
#   url_type: "lifecycle" | "licensing" | "product_hub"

SEED_HUBS = [
    # ── BMC Software ─────────────────────────────────────────────
    # docs.bmc.com is reachable; www.bmc.com often returns 403 to direct HTTP
    ("BMC Software", "https://docs.bmc.com/xwiki/bin/view/Standalone/Product-Support/productinfo/", "product_hub"),
    ("BMC Software", "https://www.bmc.com/support/resources/bmc-product-support-policy.html", "lifecycle"),
    ("BMC Software", "https://www.bmc.com/support/review-policies.html", "lifecycle"),

    # ── Microsoft ────────────────────────────────────────────────
    ("Microsoft", "https://learn.microsoft.com/en-us/lifecycle/products/", "product_hub"),
    ("Microsoft", "https://learn.microsoft.com/en-us/lifecycle/", "lifecycle"),
    ("Microsoft", "https://learn.microsoft.com/en-us/lifecycle/faq/general-lifecycle", "lifecycle"),

    # ── Autodesk ─────────────────────────────────────────────────
    ("Autodesk", "https://www.autodesk.com/support/account/manage/subscription-software/versions/support-lifecycle", "lifecycle"),
    ("Autodesk", "https://www.autodesk.com/support/account/manage/subscription-software/previous-versions/support-lifecycle", "lifecycle"),
    ("Autodesk", "https://www.autodesk.com/support/account/manage/subscription-software/previous-versions/eligible-product-list", "product_hub"),
    ("Autodesk", "https://www.autodesk.com/support/account/manage/subscription-software/retired-products", "product_hub"),

    # ── Oracle ───────────────────────────────────────────────────
    ("Oracle", "https://www.oracle.com/support/lifetime-support/", "lifecycle"),
    ("Oracle", "https://www.oracle.com/us/support/lifetime-support/", "lifecycle"),
    ("Oracle", "https://www.oracle.com/support/lifetime-support/resources.html", "product_hub"),

    # ── Red Hat ──────────────────────────────────────────────────
    ("Red Hat", "https://access.redhat.com/support/policy/updates/errata", "lifecycle"),
    ("Red Hat", "https://access.redhat.com/product-life-cycles", "product_hub"),

    # ── Cisco ────────────────────────────────────────────────────
    ("Cisco", "https://www.cisco.com/c/en/us/products/eos-eol-listing.html", "product_hub"),

    # ── VMware / Broadcom ────────────────────────────────────────
    ("VMware", "https://lifecycle.vmware.com/", "product_hub"),
    ("VMware", "https://endofgeneralsupport.vmware.com/", "lifecycle"),
]


# ─── OEM Domain Registry ───────────────────────────────────────────
# Used by the crawler to enforce domain-lock.
# key: normalized OEM name → list of trusted domains

OEM_DOMAINS = {
    "bmc software":  ["bmc.com", "docs.bmc.com", "webapps.bmc.com", "community.bmc.com"],
    "microsoft":     ["microsoft.com", "learn.microsoft.com", "techcommunity.microsoft.com", "support.microsoft.com"],
    "autodesk":      ["autodesk.com", "knowledge.autodesk.com", "forums.autodesk.com"],
    "oracle":        ["oracle.com", "docs.oracle.com", "support.oracle.com", "community.oracle.com"],
    "red hat":       ["redhat.com", "access.redhat.com", "developers.redhat.com"],
    "cisco":         ["cisco.com", "community.cisco.com"],
    "vmware":        ["vmware.com", "broadcom.com", "docs.vmware.com", "knowledge.broadcom.com"],
    "broadcom":      ["broadcom.com", "vmware.com", "support.broadcom.com", "knowledge.broadcom.com"],
    "ibm":           ["ibm.com", "community.ibm.com", "developer.ibm.com", "docs.kyndryl.com"],
    "sap":           ["sap.com", "community.sap.com", "help.sap.com"],
    "canonical":     ["ubuntu.com", "canonical.com", "discourse.ubuntu.com"],
    "aws":           ["aws.amazon.com", "amazon.com", "docs.aws.amazon.com"],
    "google":        ["cloud.google.com", "google.com", "support.google.com"],
    "hpe":           ["hpe.com", "support.hpe.com", "community.hpe.com"],
    "dell":          ["dell.com", "support.dell.com"],
    "juniper":       ["juniper.net", "community.juniper.net", "supportportal.juniper.net"],
    "palo alto":     ["paloaltonetworks.com", "live.paloaltonetworks.com"],
    "fortinet":      ["fortinet.com", "docs.fortinet.com", "community.fortinet.com"],
    "f5":            ["f5.com", "my.f5.com", "support.f5.com", "community.f5.com"],
    "adobe":         ["adobe.com", "helpx.adobe.com", "community.adobe.com"],
    "salesforce":    ["salesforce.com", "help.salesforce.com", "trailblazer.salesforce.com"],
    "servicenow":    ["servicenow.com", "docs.servicenow.com", "community.servicenow.com"],
    "splunk":        ["splunk.com", "docs.splunk.com", "community.splunk.com"],
    "elastic":       ["elastic.co", "discuss.elastic.co"],
}


def get_oem_domains(oem: str) -> list[str]:
    """
    Trusted domains: static seed_data registry + dynamically discovered (DB).
    """
    from oem_discovery import get_merged_oem_domains

    return get_merged_oem_domains(oem)


# Known-bad hub URLs (404 placeholders) — deactivated on each seed run
RETIRED_HUB_URLS = [
    "https://www.bmc.com/it-solutions/support-702702702702702.html",
    "https://www.oracle.com/us/support/library/lifetime-support-technology-702702702702702702702702.pdf",
]


def seed_database():
    """Load seed hub URLs and retire known-bad entries. Idempotent."""
    db.init_db()
    for bad_url in RETIRED_HUB_URLS:
        db.deactivate_hub_url(bad_url)
    count = 0
    for oem, url, url_type in SEED_HUBS:
        db.add_hub_url(oem, url, url_type)
        count += 1
    print(f"  [OK] Seeded {count} hub URLs for {len(set(o for o, _, _ in SEED_HUBS))} OEMs")


if __name__ == "__main__":
    seed_database()

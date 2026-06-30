"""
Layer 1 — endoflife.date REST API (https://endoflife.date/docs/api).

- GET /api/{slug}.json returns release cycles with eol/support dates
- SLUG_MAP + fuzzy match resolve product names to slugs
- confidence fixed at 0.90; licensing/chargeability always null
- Used before crawl/search because it is fast and structured
"""

import re
import requests
from typing import Optional
from datetime import datetime
from models import ProductLifecycle
from fuzzy_match import match_registry_value, normalize_key, phrase_matches
from version_utils import extract_version

BASE_URL = "https://endoflife.date/api"

# Maps common product/oem names → endoflife.date slugs
SLUG_MAP = {
    # OS
    "ubuntu": "ubuntu",
    "centos": "centos",
    "debian": "debian",
    "rhel": "rhel",
    "red hat enterprise linux": "rhel",
    "fedora": "fedora",
    "windows server": "windows-server",
    "windows 10": "windows",
    "windows 11": "windows",
    "macos": "macos",
    "alpine linux": "alpine",
    "amazon linux": "amazon-linux",
    "rocky linux": "rocky-linux",
    "alma linux": "almalinux",
    "oracle linux": "oracle-linux",
    "suse linux enterprise server": "sles",
    "sles": "sles",
    "opensuse": "opensuse",
    # Databases
    "mysql": "mysql",
    "postgresql": "postgresql",
    "postgres": "postgresql",
    "mongodb": "mongodb",
    "redis": "redis",
    "mariadb": "mariadb",
    "mssql": "mssql",
    "sql server": "mssql",
    "microsoft sql server": "mssql",
    "oracle database": "oracle-database",
    "couchbase": "couchbase-server",
    "neo4j": "neo4j",
    # Runtimes & Languages
    "nodejs": "nodejs",
    "node.js": "nodejs",
    "node": "nodejs",
    "python": "python",
    "java": "java",
    "go": "go",
    "golang": "go",
    "ruby": "ruby",
    "php": "php",
    ".net": "dotnet",
    "dotnet": "dotnet",
    ".net framework": "dotnetfx",
    "rust": "rust",
    "kotlin": "kotlin",
    "swift": "swift",
    # Platforms & Frameworks
    "kubernetes": "kubernetes",
    "k8s": "kubernetes",
    "django": "django",
    "rails": "rails",
    "ruby on rails": "rails",
    "react": "react",
    "angular": "angular",
    "vue": "vue",
    "spring boot": "spring-boot",
    "spring framework": "spring-framework",
    "laravel": "laravel",
    "flask": "flask",
    "express": "express",
    # Infra & Tools
    "nginx": "nginx",
    "apache": "apache",
    "apache http server": "apache",
    "tomcat": "tomcat",
    "elasticsearch": "elasticsearch",
    "ansible": "ansible",
    "jenkins": "jenkins",
    "terraform": "terraform",
    "docker": "docker-engine",
    "docker engine": "docker-engine",
    "gitlab": "gitlab",
    "grafana": "grafana",
    "prometheus": "prometheus",
    "rabbitmq": "rabbitmq",
    "kafka": "kafka",
    "consul": "consul",
    "vault": "vault",
    "nomad": "nomad",
    "packer": "packer",
    # Virtualization
    "vsphere": "vmware-vsphere",
    "vmware vsphere": "vmware-vsphere",
    "esxi": "esxi",
    "vmware esxi": "esxi",
    "vcenter": "vcenter",
    # Microsoft Products
    "office": "office",
    "microsoft office": "office",
    "exchange server": "exchange",
    "sharepoint server": "sharepoint",
    "visual studio": "visual-studio",
    "powershell": "powershell",
    # Networking
    "ios": "ios",
    "cisco ios": "ios",
    # Others
    "wordpress": "wordpress",
    "drupal": "drupal",
    "joomla": "joomla",
    "magento": "magento",
    # BMC
    "control-m": "controlm",
    "control m": "controlm",
    "controlm": "controlm",
    "bmc control-m": "controlm",
    "bmc control m": "controlm",
}


def _get_all_slugs() -> list[str]:
    """Fetch full product list from endoflife.date."""
    try:
        r = requests.get(f"{BASE_URL}/all.json", timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return []


def _name_without_version(product_name: str) -> str:
    """Strip trailing version tokens so slug lookup is not polluted (e.g. 9.0.22 -> 9022)."""
    import re
    name = re.sub(r"\s+v?\d+(?:\.\d+)*\s*$", "", product_name, flags=re.IGNORECASE).strip()
    name = re.sub(r"\s+", " ", name)
    return name


def _find_slug(product_name: str) -> Optional[str]:
    """Resolve product name to endoflife.date slug (boundary-aware)."""
    normalized = normalize_key(product_name)
    base = normalize_key(_name_without_version(product_name))
    for candidate in (normalized, base):
        slug = match_registry_value(candidate, SLUG_MAP)
        if slug:
            return slug
    if base:
        return base.replace(" ", "-")
    return normalized.replace(" ", "-") if normalized else None


def _find_cycle_for_version(cycles: list, version: str) -> Optional[dict]:
    for cycle in cycles:
        cycle_ver = str(cycle.get("cycle", ""))
        if not cycle_ver:
            continue
        if cycle_ver == version:
            return cycle
        if version.startswith(cycle_ver + "."):
            return cycle
        if cycle_ver.startswith(version + "."):
            return cycle
    return None


def _fetch_cycles(product_name: str) -> tuple[Optional[str], list]:
    """Return (slug, cycles list) or (None, [])."""
    slug = _find_slug(product_name)
    if not slug:
        return None, []
    try:
        r = requests.get(f"{BASE_URL}/{slug}.json", timeout=10)
        if r.status_code != 200:
            all_slugs = _get_all_slugs()
            base = _name_without_version(product_name).lower()
            tokens = re.sub(r"[^a-z0-9]+", "", base)
            matched = next(
                (
                    s for s in all_slugs
                    if s in base.replace(" ", "-")
                    or s in base.replace(" ", "")
                    or (tokens and tokens in re.sub(r"[^a-z0-9]+", "", s))
                    or re.sub(r"[^a-z0-9]+", "", s) in tokens
                ),
                None,
            )
            if not matched:
                return slug, []
            slug = matched
            r = requests.get(f"{BASE_URL}/{slug}.json", timeout=10)
            if r.status_code != 200:
                return slug, []
        cycles = r.json()
        if cycles and isinstance(cycles, list):
            return slug, cycles
    except Exception:
        pass
    return slug, []


def get_latest_cycle_version(product_name: str) -> Optional[str]:
    """Return the latest cycle version string from endoflife.date, or None."""
    _, cycles = _fetch_cycles(product_name)
    if not cycles:
        return None
    return str(cycles[0].get("cycle", "")) or None


def search_endoflife(
    product_name: str,
    oem: str,
    version: Optional[str] = None,
    version_auto_resolved: bool = False,
) -> Optional[ProductLifecycle]:
    """
    Query endoflife.date for a product.
    Returns ProductLifecycle if found, None if not.
    """
    slug, cycles = _fetch_cycles(product_name)
    if not slug or not cycles:
        return None

    try:
        caller_version = version
        from_name = extract_version(product_name)
        version = version or from_name
        version_was_requested = caller_version is not None or from_name is not None
        target_cycle = None

        if version:
            target_cycle = _find_cycle_for_version(cycles, version)
            if not target_cycle and version_was_requested:
                print(f"    [endoflife.date] Version '{version}' not in catalog — skip Layer 1")
                return None

        if not target_cycle:
            target_cycle = cycles[0]
            version = str(target_cycle.get("cycle", ""))
            version_auto_resolved = True

        eol = target_cycle.get("eol")
        support = target_cycle.get("support")
        eoas = target_cycle.get("extendedSupport") or target_cycle.get("eoas")

        # endoflife.date sometimes returns boolean True for "still active"
        # and False for "not applicable"
        def clean_date(val):
            if val is None or val is True or val is False:
                return None
            return str(val)

        return ProductLifecycle(
            product_name=product_name,
            oem=oem,
            end_of_support=clean_date(support),
            end_of_extended_support=clean_date(eoas),
            end_of_life=clean_date(eol),
            licensing_model=None,
            chargeability_type=None,
            source_url=f"https://endoflife.date/{slug}" + (
                f"/{target_cycle.get('cycle', '')}" if target_cycle.get("cycle") else ""
            ),
            confidence=0.90,
            data_source="endoflife.date",
            last_verified=datetime.now().isoformat(),
            resolved_version=version,
            version_auto_resolved=version_auto_resolved,
            date_precision="exact",
        )

    except Exception as e:
        print(f"    [endoflife.date] Error: {e}")
        return None

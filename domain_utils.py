"""
Shared hostname / domain helpers (single implementation used by crawler, search, extractor,
engine and OEM discovery).
"""

import re
from urllib.parse import urlparse

# Hosts serving user-generated content: useful to crawl, but never "official OEM" evidence.
_UGC_HOST_PREFIXES = (
    "community.", "communities.", "forum.", "forums.", "discuss.", "discourse.",
    "techcommunity.", "trailblazer.", "live.", "answers.", "stackoverflow.",
)

# Second-level labels used under two-letter ccTLDs (example.co.uk, example.com.au, ...).
_MULTI_PART_SLDS = frozenset({"co", "com", "net", "org", "gov", "edu", "ac", "ne", "or", "go", "gv"})


def hostname(url_or_host: str) -> str:
    """Lowercased hostname for a URL (or a bare host), without port or trailing dot."""
    value = (url_or_host or "").strip()
    if "://" in value:
        try:
            value = urlparse(value).hostname or ""
        except ValueError:
            return ""
    else:
        value = value.split("/", 1)[0].split(":", 1)[0]
    return value.lower().rstrip(".")


def host_matches(host_or_url: str, domain: str) -> bool:
    host = hostname(host_or_url)
    domain = domain.lower().strip().lstrip(".")
    return bool(host and domain) and (host == domain or host.endswith("." + domain))


def host_in_domains(url_or_host: str, domains) -> bool:
    if not domains:
        return False
    return any(host_matches(url_or_host, d) for d in domains)


def is_user_generated_host(url_or_host: str) -> bool:
    host = hostname(url_or_host)
    return any(host.startswith(p) for p in _UGC_HOST_PREFIXES)


def is_official_oem_url(url: str, domains) -> bool:
    """On a trusted OEM domain and not a community/forum host."""
    return host_in_domains(url, domains) and not is_user_generated_host(url)


def registrable_domain(url_or_host: str) -> str:
    """Approximate registrable domain: docs.acme.com -> acme.com, shop.acme.co.uk -> acme.co.uk."""
    host = hostname(url_or_host)
    if host.startswith("www."):
        host = host[4:]
    parts = [p for p in host.split(".") if p]
    if len(parts) <= 2:
        return ".".join(parts)
    if len(parts[-1]) == 2 and parts[-2] in _MULTI_PART_SLDS:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def name_tokens(name: str, min_len: int = 3) -> list[str]:
    """Alphanumeric tokens of a vendor name, plus its initials for multi-word names
    ('Hewlett Packard Enterprise' -> [..., 'hpe'])."""
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    tokens = [w for w in words if len(w) >= min_len]
    if len(words) >= 2:
        initials = "".join(w[0] for w in words)
        if len(initials) >= 2:
            tokens.append(initials)
    return tokens

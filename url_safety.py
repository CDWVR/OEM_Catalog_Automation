"""SSRF mitigation — block private/loopback URLs before outbound HTTP."""

import ipaddress
import re
from urllib.parse import urlparse

_BLOCKED_HOSTS = frozenset({"localhost", "metadata.google.internal", "metadata.google"})

_PRIVATE_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]


def is_safe_url(url: str) -> bool:
    if not url or not isinstance(url, str):
        return False
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host or parsed.username or parsed.password:
        return False
    h = host.lower().strip("[]")
    if h in _BLOCKED_HOSTS or h.endswith(".localhost"):
        return False
    try:
        addr = ipaddress.ip_address(h)
        return not any(
            addr in net or addr.is_loopback or addr.is_private or addr.is_link_local
            for net in _PRIVATE_NETS
        )
    except ValueError:
        pass
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", h):
        try:
            addr = ipaddress.ip_address(h)
            return not (addr.is_private or addr.is_loopback)
        except ValueError:
            return False
    return True

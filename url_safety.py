"""
SSRF mitigation — only fetch URLs that resolve to public (globally routable) addresses.

- is_safe_url(): scheme/credential checks, then resolves the hostname and rejects it if ANY
  resolved address is not globally routable (loopback, RFC1918, link-local / cloud metadata,
  CGNAT, reserved, multicast, IPv4-mapped IPv6 of those, ...). Resolving the name also defeats
  numeric tricks such as http://2130706433/, http://0x7f000001/ and http://127.1/.
- safe_get(): requests wrapper that follows redirects manually, re-validating every hop, and
  caps the (decompressed) response size so a single huge file cannot exhaust memory.

Residual risk: DNS can change between the check and the connect (rebinding). Re-checking every
redirect hop narrows the window; run the crawler on a host without access to sensitive internal
services if that matters for your deployment.
"""

import ipaddress
import os
import socket
from typing import Optional
from urllib.parse import urljoin, urlparse

_BLOCKED_HOSTS = frozenset({"localhost", "metadata.google.internal", "metadata.google", "metadata"})

DEFAULT_MAX_BYTES = 20 * 1024 * 1024


class UnsafeURLError(Exception):
    """URL (or a redirect target) points at a non-public address."""


class ResponseTooLarge(Exception):
    """Response body exceeded the configured byte cap."""


def max_response_bytes() -> int:
    try:
        return max(1024 * 1024, int(os.getenv("SCRAPE_MAX_BYTES", str(DEFAULT_MAX_BYTES))))
    except ValueError:
        return DEFAULT_MAX_BYTES


def _ip_is_public(ip_str: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip_str.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address):
        mapped = addr.ipv4_mapped or addr.sixtofour
        if mapped is not None:
            addr = mapped
    return addr.is_global and not addr.is_multicast


def _resolve(host: str) -> set[str]:
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return {info[4][0] for info in infos}


def is_safe_url(url: str) -> bool:
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url.strip())
        host = parsed.hostname
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if not host or parsed.username or parsed.password:
        return False
    h = host.lower().strip("[]").rstrip(".")
    if h in _BLOCKED_HOSTS or h.endswith(".localhost") or h.endswith(".internal"):
        return False
    try:
        ipaddress.ip_address(h)
        return _ip_is_public(h)
    except ValueError:
        pass
    try:
        ips = _resolve(h)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    return bool(ips) and all(_ip_is_public(ip) for ip in ips)


def safe_get(
    session,
    url: str,
    *,
    timeout: float = 20,
    max_bytes: Optional[int] = None,
    max_redirects: int = 5,
    **kwargs,
):
    """
    GET with per-hop SSRF validation and a response size cap.
    `session` is a requests.Session (or the requests module).
    Raises UnsafeURLError / ResponseTooLarge; requests exceptions propagate.
    """
    limit = max_bytes or max_response_bytes()
    current = url
    for _ in range(max_redirects + 1):
        if not is_safe_url(current):
            raise UnsafeURLError(current)
        r = session.get(current, timeout=timeout, allow_redirects=False, stream=True, **kwargs)
        if r.is_redirect:
            location = r.headers.get("Location")
            r.close()
            if not location:
                raise UnsafeURLError(f"redirect without Location from {current}")
            current = urljoin(current, location)
            continue
        declared = r.headers.get("Content-Length", "")
        if declared.isdigit() and int(declared) > limit:
            r.close()
            raise ResponseTooLarge(f"{declared} bytes > {limit}")
        chunks: list[bytes] = []
        total = 0
        for chunk in r.iter_content(chunk_size=65536):
            total += len(chunk)
            if total > limit:
                r.close()
                raise ResponseTooLarge(f"> {limit} bytes")
            chunks.append(chunk)
        r._content = b"".join(chunks)
        r._content_consumed = True
        return r
    raise UnsafeURLError(f"too many redirects starting at {url}")

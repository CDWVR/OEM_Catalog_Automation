"""Offline tests for SSRF protection, output sanitising, and source-trust rules.
Run: python -m unittest discover -s tests"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import url_safety  # noqa: E402
from domain_utils import (  # noqa: E402
    host_in_domains, is_official_oem_url, registrable_domain,
)
from oem_discovery import _infer_domains_from_urls  # noqa: E402
from output_safety import excel_safe  # noqa: E402


def _fake_resolve(table):
    def resolve(host):
        if host not in table:
            raise url_safety.socket.gaierror("no such host")
        return set(table[host])
    return resolve


class SafeURL(unittest.TestCase):
    def setUp(self):
        table = {
            "2130706433": ["127.0.0.1"],
            "0x7f000001": ["127.0.0.1"],
            "127.1": ["127.0.0.1"],
            "localtest.me": ["127.0.0.1"],
            "rebind.example": ["93.184.215.14", "10.0.0.5"],
            "learn.microsoft.com": ["13.107.246.40"],
        }
        patcher = mock.patch.object(url_safety, "_resolve", side_effect=_fake_resolve(table))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_blocks_internal_targets(self):
        for url in [
            "http://127.0.0.1/", "http://2130706433/", "http://0x7f000001/", "http://127.1/",
            "http://0.0.0.0/", "http://[::1]/", "http://[::ffff:127.0.0.1]/",
            "http://169.254.169.254/latest/meta-data", "http://100.64.0.1/",
            "http://10.1.2.3/", "http://localtest.me/", "http://rebind.example/",
            "http://localhost:8080/", "http://metadata.google.internal/",
            "http://user:pw@learn.microsoft.com/", "file:///etc/passwd", "ftp://example.com/",
            "http://unresolvable.invalid/",
        ]:
            self.assertFalse(url_safety.is_safe_url(url), url)

    def test_allows_public(self):
        self.assertTrue(url_safety.is_safe_url("https://learn.microsoft.com/en-us/lifecycle/"))
        self.assertTrue(url_safety.is_safe_url("https://93.184.215.14/"))


class _FakeResponse:
    def __init__(self, status=200, headers=None, body=b""):
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self.closed = False

    @property
    def is_redirect(self):
        return self.status_code in (301, 302, 303, 307, 308) and "Location" in self.headers

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        self.closed = True


class _FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.requested = []

    def get(self, url, **kwargs):
        assert kwargs.get("allow_redirects") is False
        self.requested.append(url)
        return self.responses[url]


class SafeGet(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(
            url_safety, "_resolve",
            side_effect=_fake_resolve({"vendor.example": ["93.184.215.14"]}),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_redirect_to_internal_is_blocked(self):
        session = _FakeSession({
            "https://vendor.example/eol": _FakeResponse(302, {"Location": "http://169.254.169.254/"}),
        })
        with self.assertRaises(url_safety.UnsafeURLError):
            url_safety.safe_get(session, "https://vendor.example/eol")
        self.assertEqual(session.requested, ["https://vendor.example/eol"])

    def test_follows_safe_relative_redirect(self):
        session = _FakeSession({
            "https://vendor.example/eol": _FakeResponse(301, {"Location": "/lifecycle"}),
            "https://vendor.example/lifecycle": _FakeResponse(200, {}, b"ok"),
        })
        r = url_safety.safe_get(session, "https://vendor.example/eol")
        self.assertEqual(r._content, b"ok")

    def test_size_cap(self):
        session = _FakeSession({
            "https://vendor.example/big.pdf": _FakeResponse(200, {}, b"x" * 5000),
            "https://vendor.example/declared": _FakeResponse(200, {"Content-Length": "999999"}),
        })
        with self.assertRaises(url_safety.ResponseTooLarge):
            url_safety.safe_get(session, "https://vendor.example/big.pdf", max_bytes=1000)
        with self.assertRaises(url_safety.ResponseTooLarge):
            url_safety.safe_get(session, "https://vendor.example/declared", max_bytes=1000)


class ExcelOutput(unittest.TestCase):
    def test_formulas_neutralised(self):
        for value in ['=HYPERLINK("http://x","y")', "+cmd|' /C calc'!A0", "-2+3", "@SUM(A1)", "\t=1"]:
            self.assertTrue(excel_safe(value).startswith("'"), value)

    def test_plain_values_unchanged(self):
        for value in ["2024-10-08", "https://learn.microsoft.com/x", "", "per-core", 0.9, None]:
            self.assertEqual(excel_safe(value), value)


class DomainTrust(unittest.TestCase):
    def test_subdomain_matching(self):
        self.assertTrue(host_in_domains("https://learn.microsoft.com/x", ["microsoft.com"]))
        self.assertFalse(host_in_domains("https://evilmicrosoft.com/x", ["microsoft.com"]))
        self.assertFalse(host_in_domains("https://microsoft.com.evil.io/x", ["microsoft.com"]))

    def test_community_hosts_are_not_official(self):
        domains = ["microsoft.com", "bmc.com"]
        self.assertTrue(is_official_oem_url("https://learn.microsoft.com/lifecycle", domains))
        self.assertFalse(is_official_oem_url("https://techcommunity.microsoft.com/t/1", domains))
        self.assertFalse(is_official_oem_url("https://community.bmc.com/s/q", domains))

    def test_registrable_domain(self):
        self.assertEqual(registrable_domain("docs.acme.com"), "acme.com")
        self.assertEqual(registrable_domain("shop.acme.co.uk"), "acme.co.uk")
        self.assertEqual(registrable_domain("www.acme.com.au"), "acme.com.au")

    def test_discovery_only_trusts_vendor_named_domains(self):
        urls = [
            "https://en.wikipedia.org/wiki/Acme",
            "https://shop.example.co.uk/acme-eol",
            "https://acme-eol-dates.com/acme",
            "https://www.acme.com/support/lifecycle",
            "https://docs.acme.com/eol",
        ]
        self.assertEqual(_infer_domains_from_urls(urls, "Acme"), ["acme.com", "docs.acme.com"])

    def test_discovery_trusts_nothing_without_a_match(self):
        self.assertEqual(_infer_domains_from_urls(["https://seo-spam.example/acme"], "Acme"), [])

    def test_discovery_handles_acronyms(self):
        got = _infer_domains_from_urls(["https://support.hpe.com/x"], "Hewlett Packard Enterprise")
        self.assertEqual(got, ["hpe.com", "support.hpe.com"])


if __name__ == "__main__":
    unittest.main()

"""Offline tests for extraction grounding, confidence scoring, and product/version matching.
Run: python -m unittest discover -s tests"""

import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# extractor imports google-genai at module load; tests never call the API.
if "google.genai" not in sys.modules:
    try:
        import google.genai  # noqa: F401
    except ImportError:
        google_pkg = sys.modules.setdefault("google", types.ModuleType("google"))
        genai_stub = types.ModuleType("google.genai")
        genai_stub.Client = object
        google_pkg.genai = genai_stub
        sys.modules["google.genai"] = genai_stub

import extractor  # noqa: E402
from date_utils import date_grounding  # noqa: E402
from product_matching import products_match  # noqa: E402
from sources import endoflife_api  # noqa: E402
from version_utils import versions_match  # noqa: E402

PAGE = (
    "Windows Server 2019 lifecycle. Start date: November 13, 2018. "
    "Mainstream End Date: 1/9/2024. Extended End Date: 01/09/2029. "
) * 20


class DateGrounding(unittest.TestCase):
    def test_formats(self):
        text = "Ends October 8, 2024; extended 14 Oct 2031; retired 08-Jan-2020; EOL 2035; Mar 2027"
        self.assertEqual(date_grounding("2024-10-08", text), "exact")
        self.assertEqual(date_grounding("2031-10-14", text), "exact")
        self.assertEqual(date_grounding("2020-01-08", text), "exact")
        self.assertEqual(date_grounding("2035", text), "exact")
        self.assertEqual(date_grounding("2027-03", text), "exact")
        self.assertEqual(date_grounding("2024-10-09", text), "partial")
        self.assertEqual(date_grounding("2039-01-01", text), "absent")

    def test_ungrounded_dates_are_dropped(self):
        data = {
            "end_of_support": "2024-01-09",
            "end_of_extended_support": "2035-01-01",   # injected / hallucinated
            "end_of_life": None,
            "release_date": "2018-11-13",
            "estimated_end_of_support": "2099-01-01",  # model-computed: never trusted
        }
        data, weak, dropped = extractor.ground_extracted_dates(data, PAGE)
        self.assertEqual(data["end_of_support"], "2024-01-09")
        self.assertIsNone(data["end_of_extended_support"])
        self.assertIsNone(data["estimated_end_of_support"])
        self.assertEqual(dropped, ["end_of_extended_support"])
        self.assertEqual(weak, 0)


class VersionAndEdition(unittest.TestCase):
    def test_versions_match(self):
        self.assertTrue(versions_match("19c", "19"))
        self.assertTrue(versions_match("8.0", "8"))
        self.assertTrue(versions_match("v15.0.0", "15"))
        self.assertTrue(versions_match("2012-R2", "2012 R2"))
        self.assertTrue(versions_match("Windows Server 2019", "2019"))
        self.assertFalse(versions_match("2016", "2019"))
        self.assertFalse(versions_match("8.4", "8"))
        self.assertFalse(versions_match("2012", "2012 R2"))

    def test_products_match(self):
        self.assertTrue(products_match("Windows Server 2019", "Microsoft Windows Server 2019", "Microsoft"))
        self.assertTrue(products_match("Oracle Database 19c", "Oracle Database", "Oracle"))
        self.assertFalse(products_match(
            "WebSphere Application Server 9.0",
            "WebSphere Application Server Network Deployment", "IBM",
        ))
        self.assertFalse(products_match("SQL Server 2019", "Azure SQL Server", "Microsoft"))


class Confidence(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(extractor, "get_oem_domains", return_value=["microsoft.com"])
        patcher.start()
        self.addCleanup(patcher.stop)

    def _score(self, url, **overrides):
        extraction = {
            "end_of_support": "2024-01-09",
            "end_of_extended_support": "2029-01-09",
            "lifecycle_status": "dates_found",
            "product_found_in_text": True,
            "product_variant_found": "Microsoft Windows Server 2019",
            "version_found": "2019",
        }
        extraction.update(overrides)
        return extractor.calculate_confidence(
            source_url=url, oem="Microsoft", extraction=extraction,
            page_text_length=len(PAGE), product_name_for_version="Windows Server 2019",
            target_version="2019",
        )

    def test_official_page_scores_high(self):
        self.assertGreaterEqual(self._score("https://learn.microsoft.com/lifecycle/x"), 0.8)

    def test_community_page_is_penalised(self):
        official = self._score("https://learn.microsoft.com/lifecycle/x")
        community = self._score("https://techcommunity.microsoft.com/t/x")
        self.assertLess(community, official * 0.6)

    def test_equivalent_version_label_not_penalised(self):
        a = self._score("https://learn.microsoft.com/x", version_found="2019")
        b = self._score("https://learn.microsoft.com/x", version_found="v2019")
        self.assertEqual(a, b)
        # A real mismatch must stay below even the partial-acceptance threshold (0.42)
        self.assertLess(self._score("https://learn.microsoft.com/x", version_found="2016"), 0.42)


class EndoflifeSlugSafety(unittest.TestCase):
    def setUp(self):
        endoflife_api._get_json.cache_clear()
        self.addCleanup(endoflife_api._get_json.cache_clear)

    def _fake_json(self, url):
        if url.endswith("/all.json"):
            return ["go", "r", "nix", "amazon-linux", "ios"]
        if url.endswith("/go.json") or url.endswith("/ios.json"):
            return [{"cycle": "1.0", "eol": "2030-01-01"}]
        if url.endswith("/amazon-linux.json"):
            return [{"cycle": "2023", "eol": "2029-06-30"}]
        return None

    def test_no_substring_slug_fallback(self):
        with mock.patch.object(endoflife_api, "_get_json", side_effect=self._fake_json):
            self.assertEqual(endoflife_api._fetch_cycles("Gong Analytics")[1], [])
            self.assertEqual(endoflife_api._fetch_cycles("Acrobat Reader")[1], [])

    def test_ambiguous_slug_requires_matching_oem(self):
        with mock.patch.object(endoflife_api, "_get_json", side_effect=self._fake_json):
            self.assertIsNone(endoflife_api.search_endoflife("IOS", "Cisco"))
            self.assertIsNotNone(endoflife_api.search_endoflife("iOS", "Apple"))


if __name__ == "__main__":
    unittest.main()

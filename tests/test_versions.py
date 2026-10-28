"""Offline tests for version parsing and cycle matching. Run: python -m unittest discover -s tests"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import version_resolver  # noqa: E402
from sources import endoflife_api  # noqa: E402
from version_utils import extract_version, strip_trailing_version  # noqa: E402

WINDOWS = [{"cycle": c} for c in ["2025", "2022", "2019", "2016", "2012-r2", "2012"]]
ORACLE = [{"cycle": c} for c in ["23", "21", "19", "18", "12.2"]]
MSSQL = [{"cycle": c} for c in ["17.0", "16.0", "13.0-sp3", "15.0", "14.0", "13.0"]]


class ExtractVersion(unittest.TestCase):
    def test_forms(self):
        cases = {
            "Windows Server 2012 R2": "2012 R2",
            "Windows Server 2012": "2012",
            "Oracle Database 19c": "19",
            "Oracle Database 11g": "11",
            "SQL Server 2019": "2019",
            "vSphere 7.0": "7.0",
            "Java SE 8": "8",
            "Control-M 9.0.21": "9.0.21",
            "Windows Server": None,
            "Microsoft Office 365": None,
        }
        for name, want in cases.items():
            self.assertEqual(extract_version(name), want, name)

    def test_strip(self):
        self.assertEqual(strip_trailing_version("Windows Server 2012 R2"), "Windows Server")
        self.assertEqual(strip_trailing_version("Oracle Database 19c"), "Oracle Database")
        self.assertEqual(strip_trailing_version("Control-M 9.0.21"), "Control-M")


class CycleMatching(unittest.TestCase):
    def test_release_suffix(self):
        self.assertEqual(endoflife_api._find_cycle_for_version(WINDOWS, "2012 R2")["cycle"], "2012-r2")
        self.assertEqual(endoflife_api._find_cycle_for_version(WINDOWS, "2012")["cycle"], "2012")

    def test_sql_server_marketing_names(self):
        self.assertEqual(endoflife_api._find_cycle_for_version(MSSQL, "2019", "mssqlserver")["cycle"], "15.0")
        self.assertEqual(endoflife_api._find_cycle_for_version(MSSQL, "2016", "mssqlserver")["cycle"], "13.0")

    def test_oracle_suffix(self):
        self.assertEqual(endoflife_api._find_cycle_for_version(ORACLE, "19")["cycle"], "19")


class NoLatestFallbackForNamedVersions(unittest.TestCase):
    """The original bug: a version that failed to parse silently became 'latest'."""

    def test_named_version_is_never_replaced_by_latest(self):
        with mock.patch.object(version_resolver, "get_latest_cycle_version", return_value="2025") as latest:
            q = version_resolver.prepare_query("Windows Server 2012 R2", "Microsoft", verbose=False)
        self.assertEqual(q.version, "2012 R2")
        self.assertFalse(q.version_auto_resolved)
        latest.assert_not_called()

    def test_unparseable_version_is_not_guessed(self):
        with mock.patch.object(version_resolver, "get_latest_cycle_version", return_value="2025") as latest:
            q = version_resolver.prepare_query("Windows Server 2012 Datacenter", "Microsoft", verbose=False)
        self.assertIsNone(q.version)
        latest.assert_not_called()

    def test_no_version_still_resolves_latest(self):
        with mock.patch.object(version_resolver, "get_latest_cycle_version", return_value="2025"):
            q = version_resolver.prepare_query("Windows Server", "Microsoft", verbose=False)
        self.assertEqual(q.version, "2025")
        self.assertTrue(q.version_auto_resolved)

    def test_layer1_refuses_latest_for_unmatched_version(self):
        with mock.patch.object(endoflife_api, "_fetch_cycles", return_value=("windows-server", WINDOWS)):
            r = endoflife_api.search_endoflife("Windows Server 2012 Datacenter", "Microsoft")
        self.assertIsNone(r)

    def test_layer1_returns_the_right_cycle(self):
        cycles = [{"cycle": "2025", "eol": "2034-11-14"}, {"cycle": "2012-r2", "eol": "2023-10-10", "support": "2018-10-09"}]
        with mock.patch.object(endoflife_api, "_fetch_cycles", return_value=("windows-server", cycles)):
            r = endoflife_api.search_endoflife("Windows Server 2012 R2", "Microsoft", version="2012 R2")
        self.assertEqual(r.end_of_life, "2023-10-10")
        self.assertFalse(r.version_auto_resolved)


if __name__ == "__main__":
    unittest.main()

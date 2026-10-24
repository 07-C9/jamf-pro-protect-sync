# ABOUTME: Tests for the sync's pure comparison and normalization logic.
# ABOUTME: API calls and AWS access are exercised by the dry-run procedure in docs/setup.md, not mocked here.

import unittest

import contextlib
import io

from lambda_function import (
    delete_computers_from_protect,
    extract_serial_numbers_from_pro,
    find_computers_to_delete,
    normalize_protect_computers,
)


class ExtractSerialsTests(unittest.TestCase):
    def test_normalizes_case_and_whitespace(self):
        computers = [{"serial_number": " c02abc123 "}]
        self.assertEqual(extract_serial_numbers_from_pro(computers), {"C02ABC123"})

    def test_accepts_alternate_key_spellings(self):
        computers = [
            {"serial_number": "AAA"},
            {"serialNumber": "BBB"},
            {"serial": "CCC"},
        ]
        self.assertEqual(extract_serial_numbers_from_pro(computers), {"AAA", "BBB", "CCC"})

    def test_skips_computers_without_serial(self):
        computers = [{"name": "no-serial"}, {"serial_number": ""}, {"serial_number": "AAA"}]
        self.assertEqual(extract_serial_numbers_from_pro(computers), {"AAA"})


class NormalizeProtectTests(unittest.TestCase):
    def test_keys_by_normalized_serial(self):
        computers = [{"serial": " aaa ", "uuid": "u1"}]
        normalized = normalize_protect_computers(computers)
        self.assertEqual(list(normalized.keys()), ["AAA"])

    def test_skips_records_without_serial(self):
        computers = [{"serial": "", "uuid": "ghost"}, {"serial": "AAA", "uuid": "u1"}]
        normalized = normalize_protect_computers(computers)
        self.assertEqual(list(normalized.keys()), ["AAA"])


class FindComputersToDeleteTests(unittest.TestCase):
    def test_deletes_only_protect_records_missing_from_pro(self):
        pro_serials = {"AAA", "BBB"}
        protect = {
            "AAA": {"serial": "AAA", "uuid": "u1"},
            "CCC": {"serial": "CCC", "uuid": "u3"},
        }
        to_delete = find_computers_to_delete(pro_serials, protect)
        self.assertEqual([c["serial"] for c in to_delete], ["CCC"])

    def test_empty_pro_inventory_marks_everything(self):
        protect = {
            "AAA": {"serial": "AAA", "uuid": "u1"},
            "BBB": {"serial": "BBB", "uuid": "u2"},
        }
        to_delete = find_computers_to_delete(set(), protect)
        self.assertEqual(len(to_delete), 2)

    def test_in_sync_inventories_delete_nothing(self):
        pro_serials = {"AAA"}
        protect = {"AAA": {"serial": "AAA", "uuid": "u1"}}
        self.assertEqual(find_computers_to_delete(pro_serials, protect), [])


class DeletionLoggingTests(unittest.TestCase):
    """Every deleted serial must appear in the log so a run can be audited afterwards."""

    def run_deletions(self, computers, failing_uuids=(), graphql_error_uuids=()):
        def api_call(instance, token, query, variables):
            if variables["uuid"] in failing_uuids:
                raise RuntimeError("delete refused")
            if variables["uuid"] in graphql_error_uuids:
                return {"data": {"deleteComputer": None}, "errors": [{"message": "not authorized"}]}
            return {"data": {"deleteComputer": {"hostName": "student-host"}}}

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            counts = delete_computers_from_protect("inst", "tok", computers, api_call=api_call)
        return counts, out.getvalue()

    def test_logs_every_deleted_serial_past_ten(self):
        computers = [{"serial": f"SER{i:03d}", "uuid": f"u{i}", "hostName": f"host{i}"} for i in range(1, 26)]
        counts, log = self.run_deletions(computers)
        self.assertEqual(counts, (25, 0))
        for c in computers:
            self.assertIn(f"Deleted serial {c['serial']}", log)

    def test_never_logs_hostnames(self):
        computers = [{"serial": "AAA", "uuid": "u1", "hostName": "host-with-a-name"}]
        _, log = self.run_deletions(computers)
        self.assertNotIn("host-with-a-name", log)
        self.assertNotIn("student-host", log)

    def test_logs_failed_serial(self):
        computers = [{"serial": "AAA", "uuid": "u1", "hostName": "h1"}, {"serial": "BBB", "uuid": "u2", "hostName": "h2"}]
        counts, log = self.run_deletions(computers, failing_uuids={"u2"})
        self.assertEqual(counts, (1, 1))
        self.assertIn("Failed to delete serial BBB", log)
        self.assertNotIn("h2", log)

    def test_graphql_error_response_counts_as_failure(self):
        computers = [{"serial": "AAA", "uuid": "u1", "hostName": "h1"}]
        counts, log = self.run_deletions(computers, graphql_error_uuids={"u1"})
        self.assertEqual(counts, (0, 1))
        self.assertIn("Failed to delete serial AAA", log)
        self.assertNotIn("Deleted serial AAA", log)


if __name__ == "__main__":
    unittest.main()

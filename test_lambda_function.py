# ABOUTME: Tests for the sync's pure comparison and normalization logic.
# ABOUTME: API calls and AWS access are exercised by the dry-run procedure in docs/setup.md, not mocked here.

import unittest

from lambda_function import (
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


if __name__ == "__main__":
    unittest.main()

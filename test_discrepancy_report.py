# ABOUTME: Tests for the discrepancy report comparison logic.
# ABOUTME: Uses real data structures shaped like Jamf Pro and Protect API responses.

import unittest

from discrepancy_report import build_report


def pro(serial, managed=True, name="mac"):
    return {"serial_number": serial, "managed": managed, "name": name}


def protect(serial, uuid="u", hostname="h", checkin="2026-07-01T00:00:00Z"):
    return {"serial": serial, "uuid": uuid, "hostName": hostname, "checkin": checkin}


class BuildReportTests(unittest.TestCase):
    def test_orphan_in_protect_not_in_pro(self):
        report = build_report(
            pro_computers=[pro("AAA")],
            protect_computers=[protect("AAA"), protect("BBB", uuid="u2")],
            now_iso="2026-07-01T12:00:00Z",
        )
        self.assertEqual([c["serial"] for c in report["protect_not_in_pro"]], ["BBB"])

    def test_pro_mac_missing_protect_agent(self):
        report = build_report(
            pro_computers=[pro("AAA"), pro("CCC")],
            protect_computers=[protect("AAA")],
            now_iso="2026-07-01T12:00:00Z",
        )
        self.assertEqual([c["serial_number"] for c in report["pro_not_in_protect"]], ["CCC"])

    def test_serial_less_protect_records_reported_not_dropped(self):
        report = build_report(
            pro_computers=[pro("AAA")],
            protect_computers=[protect("AAA"), protect("", uuid="ghost1"), protect(None, uuid="ghost2")],
            now_iso="2026-07-01T12:00:00Z",
        )
        self.assertEqual(
            sorted(c["uuid"] for c in report["protect_missing_serial"]), ["ghost1", "ghost2"]
        )

    def test_duplicate_serials_in_protect_detected(self):
        report = build_report(
            pro_computers=[pro("AAA")],
            protect_computers=[protect("AAA", uuid="u1"), protect("aaa ", uuid="u2")],
            now_iso="2026-07-01T12:00:00Z",
        )
        self.assertEqual(list(report["protect_duplicate_serials"].keys()), ["AAA"])
        self.assertEqual(len(report["protect_duplicate_serials"]["AAA"]), 2)

    def test_serials_normalized_case_and_whitespace(self):
        report = build_report(
            pro_computers=[pro(" aaa ")],
            protect_computers=[protect("AAA")],
            now_iso="2026-07-01T12:00:00Z",
        )
        self.assertEqual(report["protect_not_in_pro"], [])
        self.assertEqual(report["pro_not_in_protect"], [])

    def test_stale_protect_checkin_flagged(self):
        report = build_report(
            pro_computers=[pro("AAA"), pro("BBB")],
            protect_computers=[
                protect("AAA", checkin="2026-06-30T00:00:00Z"),
                protect("BBB", uuid="u2", checkin="2026-03-01T00:00:00Z"),
            ],
            now_iso="2026-07-01T12:00:00Z",
            stale_days=30,
        )
        self.assertEqual([c["serial"] for c in report["protect_stale_checkin"]], ["BBB"])

    def test_unmanaged_pro_computers_counted(self):
        report = build_report(
            pro_computers=[pro("AAA", managed=False), pro("BBB")],
            protect_computers=[protect("AAA"), protect("BBB", uuid="u2")],
            now_iso="2026-07-01T12:00:00Z",
        )
        self.assertEqual([c["serial_number"] for c in report["pro_unmanaged"]], ["AAA"])


if __name__ == "__main__":
    unittest.main()

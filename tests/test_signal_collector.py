"""Small checks for the forward collector's hard-to-see edges."""

from datetime import datetime
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "signal"))

from collector import OoklaError, classify_error, collect_once, next_boundary, parse_ookla, should_try_fallback  # noqa: E402


class SignalCollectorTest(unittest.TestCase):
    def test_ookla_json_uses_bytes_per_second_and_server_identity(self):
        payload = (
            '{"download":{"bandwidth":100000000},"upload":{"bandwidth":50000000},'
            '"ping":{"latency":4.2,"jitter":0.4},"packetLoss":0,'
            '"server":{"id":"31903","name":"Google Fiber",'
            '"location":"Salt Lake City, UT","country":"United States"}}'
        )
        result = parse_ookla(payload, "31903")
        self.assertEqual(result["download_mbps"], 800.0)
        self.assertEqual(result["upload_mbps"], 400.0)
        self.assertEqual(result["server_id"], "31903")
        with self.assertRaises(OoklaError):
            parse_ookla(payload, "2185")

    def test_next_boundary_rolls_into_next_day(self):
        self.assertEqual(
            next_boundary(datetime(2026, 1, 1, 23, 45), 15),
            datetime(2026, 1, 2, 0, 0),
        )

    def test_only_server_specific_failures_use_fallback(self):
        self.assertEqual(classify_error("Unable to connect to servers to test latency."), "server_selection_failed")
        self.assertEqual(classify_error("Configuration failed (403)"), "service_rejected")
        self.assertEqual(classify_error("Configuration - Couldn't resolve host name"), "dns_resolution_failed")
        self.assertTrue(should_try_fallback({"status": "failure", "failure_kind": "server_selection_failed"}))
        self.assertTrue(should_try_fallback({"status": "failure", "failure_kind": "server_mismatch"}))
        self.assertFalse(should_try_fallback({"status": "failure", "failure_kind": "dns_resolution_failed"}))
        self.assertFalse(should_try_fallback({"status": "success"}))

    def test_collect_once_stops_on_global_failure(self):
        conn = type("Connection", (), {"commit": lambda self: None})()
        dns_failure = {"status": "failure", "failure_kind": "dns_resolution_failed", "command": []}
        server_failure = {"status": "failure", "failure_kind": "server_selection_failed", "command": []}
        success = {"status": "success", "command": [], "server_id": "2"}
        with patch("collector.insert_attempt"), patch("collector.run_attempt", side_effect=[dns_failure, success]) as run:
            collect_once(conn, "speedtest", ("1", "2"), 1, "Ookla")
        self.assertEqual(run.call_count, 1)
        with patch("collector.insert_attempt"), patch("collector.run_attempt", side_effect=[server_failure, success]) as run:
            collect_once(conn, "speedtest", ("1", "2"), 1, "Ookla")
        self.assertEqual(run.call_count, 2)


if __name__ == "__main__":
    unittest.main()

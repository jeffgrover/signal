"""Small checks for the forward collector's hard-to-see edges."""

from datetime import datetime
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "signal"))

from collector import OoklaError, classify_error, collect_once, main, next_boundary, parse_ookla, send_heartbeat, should_try_fallback  # noqa: E402


PING_URL = "https://hc-ping.com/123e4567-e89b-12d3-a456-426614174000"


class SignalCollectorTest(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"SIGNAL_HEALTHCHECK_URL": ""})
        environment.start()
        self.addCleanup(environment.stop)

    def test_heartbeat_is_optional_private_and_checks_acceptance(self):
        with patch("collector.urlopen") as open_url, patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertIsNone(send_heartbeat(""))
            open_url.assert_not_called()
            response = open_url.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = b"OK"
            self.assertTrue(send_heartbeat(PING_URL))
            request = open_url.call_args.args[0]
            self.assertEqual(request.full_url, PING_URL)
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(request.data, b"")
            self.assertEqual(open_url.call_args.kwargs, {"timeout": 10})
            response.read.return_value = b"OK (not found)"
            self.assertFalse(send_heartbeat(PING_URL))
            response.read.return_value = b"OK (rate limited)"
            self.assertFalse(send_heartbeat(PING_URL))
            response.status = 503
            response.read.return_value = b"OK"
            self.assertFalse(send_heartbeat(PING_URL))
            open_url.side_effect = URLError(PING_URL)
            self.assertFalse(send_heartbeat(PING_URL))
            open_url.side_effect = TimeoutError(PING_URL)
            self.assertFalse(send_heartbeat(PING_URL))
            self.assertNotIn(PING_URL, output.getvalue())
            self.assertIn("delivery failed", output.getvalue())

    def test_heartbeat_rejects_insecure_wrong_host_and_non_success_urls(self):
        for url in (PING_URL.replace("https:", "http:"), PING_URL.replace("hc-ping.com", "example.com"),
                    PING_URL + "/fail", PING_URL + "?create=1", "https://hc-ping.com/not-a-uuid"):
            with self.subTest(url=url), patch("collector.urlopen") as open_url, patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertFalse(send_heartbeat(url))
                open_url.assert_not_called()
                self.assertNotIn(url, output.getvalue())

    def test_heartbeat_follows_commit_for_both_success_and_recorded_failure(self):
        for status in ("success", "failure"):
            with self.subTest(status=status), patch.dict(os.environ, {"SIGNAL_HEALTHCHECK_URL": PING_URL}), \
                    patch("collector.insert_attempt") as insert, patch("collector.run_attempt", return_value={"status": status}), \
                    patch("collector.send_heartbeat", return_value=False) as heartbeat:
                conn = Mock()
                calls = Mock()
                calls.attach_mock(insert, "insert")
                calls.attach_mock(conn.commit, "commit")
                calls.attach_mock(heartbeat, "heartbeat")
                _, succeeded = collect_once(conn, "speedtest", ("1",), 1, "Ookla")
                self.assertEqual(succeeded, status == "success")
                self.assertEqual([call[0] for call in calls.mock_calls], ["insert", "commit", "heartbeat"])
                heartbeat.assert_called_once_with(PING_URL)

    def test_no_heartbeat_if_database_write_or_commit_fails(self):
        for failure_at in ("insert", "commit"):
            with self.subTest(failure_at=failure_at), patch("collector.insert_attempt") as insert, \
                    patch("collector.run_attempt", return_value={"status": "success"}), patch("collector.send_heartbeat") as heartbeat:
                conn = Mock()
                (insert if failure_at == "insert" else conn.commit).side_effect = RuntimeError("storage failure")
                with self.assertRaises(RuntimeError):
                    collect_once(conn, "speedtest", ("1",), 1, "Ookla")
                heartbeat.assert_not_called()

    def test_empty_server_list_cannot_send_a_heartbeat(self):
        with patch("collector.send_heartbeat") as heartbeat:
            _, succeeded = collect_once(Mock(), "speedtest", (), 1, "Ookla")
            self.assertFalse(succeeded)
            heartbeat.assert_not_called()

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
        self.assertEqual(classify_error("Configuration - Couldn't connect (Network is unreachable)"), "network_unreachable")
        self.assertEqual(classify_error("Configuration - No route to host"), "no_route_to_host")
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
            _, succeeded = collect_once(conn, "speedtest", ("1", "2"), 1, "Ookla")
        self.assertEqual(run.call_count, 1)
        self.assertFalse(succeeded)
        with patch("collector.insert_attempt"), patch("collector.run_attempt", side_effect=[server_failure, success]) as run:
            _, succeeded = collect_once(conn, "speedtest", ("1", "2"), 1, "Ookla")
        self.assertEqual(run.call_count, 2)
        self.assertTrue(succeeded)

    def test_once_exits_nonzero_when_collection_fails(self):
        with (
            patch.object(sys, "argv", ["collector.py", "--once"]),
            patch("collector.verify_ookla", return_value="Ookla"),
            patch("collector.connect"),
            patch("collector.ensure_schema"),
            patch("collector.collect_once", return_value=("run-id", False)),
        ):
            self.assertEqual(main(), 1)


if __name__ == "__main__":
    unittest.main()

"""Small checks for the forward collector's hard-to-see edges."""

from datetime import datetime
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "signal"))

from collector import OoklaError, next_boundary, parse_ookla  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()

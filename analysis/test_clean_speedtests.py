"""Run with: python3 -m unittest discover -s analysis -v"""

from datetime import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from clean_speedtests import build, classify_error, clean_row, fingerprint, normalize


class CleaningChecks(unittest.TestCase):
    def row(self, **changes):
        return {"source_rowid": 1, "timestamp": "2025-11-02T01:00:01.002000",
                "download": 100.0, "upload": 10.0, "ping": 12.0, "error": None, **changes}

    def entry(self, payload):
        return (datetime(2025, 11, 2, 1, 0, 1), 42, normalize(payload))

    def python_payload(self, **changes):
        return {"download": 100_000_000, "upload": 10_000_000, "ping": 12, **changes}

    def clean(self, row, *entries):
        return clean_row(row, entries, [entry[0] for entry in entries])

    def test_verified_units_and_slow_values(self):
        ookla = self.entry({"download": {"bandwidth": 12_500_000},
                            "upload": {"bandwidth": 1_250_000}, "ping": {"latency": 12}})
        result = self.clean(self.row(download=12.5, upload=1.25), ookla)
        self.assertEqual((result["download_mbps"], result["bandwidth_status"]), (100, "corrected_x8"))
        slow = self.entry(self.python_payload(download=500_000))
        self.assertEqual(self.clean(self.row(download=0.5), slow)["download_mbps"], 0.5)

    def test_latency_invalidated_independently(self):
        for ping in (600044.001, 1200017.134, 1800000):
            result = self.clean(self.row(ping=ping), self.entry(self.python_payload(ping=ping)))
            self.assertIsNone(result["latency_ms"])
            self.assertEqual(result["download_mbps"], 100)
        high = self.clean(self.row(ping=1549.659), self.entry(self.python_payload(ping=1549.659)))
        self.assertEqual(high["latency_ms"], 1549.659)

    def test_errors_preserved_and_only_parser_result_recovered(self):
        entry = self.entry(self.python_payload())
        parser_row = self.row(download=None, upload=None, ping=None,
                              error="unsupported operand type(s) for /: 'dict' and 'int'")
        result = self.clean(parser_row, entry)
        self.assertEqual(result["bandwidth_status"], "recovered_from_log")
        self.assertEqual(result["failure_kind"], "collector_parse_error")
        result = self.clean({**parser_row, "error": "No route to host"}, entry)
        self.assertIsNone(result["download_mbps"])
        self.assertEqual(result["connection_evidence"], "routing")
        self.assertEqual(classify_error("Unable to connect to servers to test latency"),
                         ("server_selection_failed", "uncertain"))

    def test_repeated_hour_and_ambiguous_matches(self):
        wrong = self.entry(self.python_payload(download=200_000_000))
        right = self.entry(self.python_payload())
        self.assertEqual(self.clean(self.row(), wrong, right)["log_match"], "unique")
        self.assertEqual(self.clean(self.row(), right, right)["log_match"], "ambiguous")
        self.assertIsNone(self.clean(self.row())["download_mbps"])

    def test_copy_provenance_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            source, log, output = (Path(folder) / name for name in ("raw.db", "test.log", "clean.db"))
            with sqlite3.connect(source) as db:
                db.execute("CREATE TABLE speedtests (timestamp TEXT,download REAL,upload REAL,ping REAL,error TEXT)")
                db.execute("INSERT INTO speedtests VALUES (?,?,?,?,?)",
                           ("2025-11-02T01:00:01.002000", 100, 10, 12, None))
            log.write_text("2025-11-02 01:00:01,000 - DEBUG - Raw speedtest output: "
                           + json.dumps(self.python_payload()) + "\n")
            original_hash = fingerprint(source)
            summary = build(source, log, output)
            self.assertEqual(summary["rows_preserved"], 1)
            self.assertEqual(fingerprint(source), original_hash)
            with sqlite3.connect(output) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM throughput_samples").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT source_log_line FROM observations").fetchone()[0], 1)
            with self.assertRaises(FileExistsError):
                build(source, log, source)
            with self.assertRaises(FileExistsError):
                build(source, log, output)


if __name__ == "__main__":
    unittest.main()

"""Immutable imports, canonical chart data, and the private static build."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "signal"))

import duckdb
from collector import insert_attempt
from db import connect, ensure_schema
from export_static import main as export_main
from import_snapshot import main as import_main, sha256
from dashboard import FIELDS, load_data


def make_sources(root):
    bandwidth, pihole = root / "clean.db", root / "pihole.db"
    with sqlite3.connect(bandwidth) as c:
        c.execute(f"CREATE TABLE observations ({','.join(FIELDS)},server_name,server_location,server_country)")
        c.execute("INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            42, "2026-01-01T12:00:00", 800, 400, None, None, "corrected_x8",
            "invalid_cli_failure_sentinel", "ookla", "31903", 100, 50, 600044, None, 99,
            "Google Fiber", "Salt Lake City", "United States",
        ))
        c.execute("INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            43, "2026-01-01T12:15:00", None, None, None, "dns_resolution_failed",
            "not_available", "not_available", "python_speedtest_cli", None,
            None, None, None, "Couldn't resolve host", 100, None, None, None,
        ))
    with sqlite3.connect(pihole) as c:
        c.executescript("""
            CREATE TABLE domain_by_id(id,domain);
            INSERT INTO domain_by_id VALUES (1,'example.com'),(2,'blocked.example');
            CREATE TABLE client_by_id(id,ip,name);
            INSERT INTO client_by_id VALUES (1,'192.0.2.10','Laptop <private>');
            CREATE TABLE forward_by_id(id,forward);
            INSERT INTO forward_by_id VALUES (1,'1.1.1.1');
            CREATE TABLE network(id,hwaddr,interface,firstSeen,lastQuery,numQueries,macVendor,aliasclient_id);
            INSERT INTO network VALUES (1,'00:00:00:00:00:01','eth0',0,0,3,'Example',NULL);
            CREATE TABLE network_addresses(network_id,ip,lastSeen,name,nameUpdated);
            INSERT INTO network_addresses VALUES (1,'192.0.2.10',0,'Laptop',0);
            CREATE TABLE query_storage(id,timestamp,type,status,domain,client,forward,reply_type,reply_time,dnssec,list_id,ede);
            INSERT INTO query_storage VALUES
                (1,1767294000,1,2,1,1,1,4,0.01,0,NULL,NULL),
                (2,1767294060,1,1,2,1,NULL,4,0.0,0,1,NULL),
                (3,1767294120,1,3,1,1,NULL,4,0.0,0,NULL,NULL);
        """)
    return bandwidth, pihole


class SignalPipelineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "signal.duckdb"
        self.bandwidth, self.pihole = make_sources(self.root)

    def run_import(self, *sources):
        with patch.object(sys, "argv", ["import_snapshot.py", "--db", str(self.db), *map(str, sources)]):
            return import_main()

    def populate(self):
        self.run_import("--bandwidth", self.bandwidth, "--pihole", self.pihole)

    def test_import_is_idempotent_and_preserves_provenance_and_forward_attempts(self):
        before = [sha256(p) for p in (self.bandwidth, self.pihole)]
        self.populate()
        with connect(self.db) as c:
            insert_attempt(c, "forward", datetime(2026, 1, 2, 18, tzinfo=timezone.utc), 1, "31903",
                           {"status": "success", "server_id": "31903", "download_mbps": 0,
                            "upload_mbps": 0, "latency_ms": 0}, "Speedtest by Ookla 1.2.0")
        self.populate()
        with connect(self.db, read_only=True) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM speedtest_attempts").fetchone()[0], 3)
            self.assertEqual(c.execute("SELECT count(*) FROM pihole_queries").fetchone()[0], 3)
            self.assertEqual(c.execute("SELECT * EXCLUDE(attempt_id) FROM speedtest_quality WHERE attempt_id='legacy-speedtest-42'").fetchone(),
                             ("corrected_x8", "invalid_cli_failure_sentinel", 100, 50, 600044, 99))
            self.assertEqual(c.execute("SELECT row_count,sha256 FROM source_metadata WHERE source_name='legacy_bandwidth'").fetchone(), (2, before[0]))
        data = load_data(self.db)
        records = [dict(zip(data["fields"], row)) for row in data["rows"]]
        self.assertEqual(records[0]["source_rowid"], 42)
        self.assertIsNone(records[0]["observed_at_utc"])
        self.assertEqual(records[0]["bandwidth_status"], "corrected_x8")
        self.assertEqual(records[-1]["client"], "ookla")
        self.assertEqual(records[-1]["download_mbps"], 0)
        self.assertIsNotNone(records[-1]["observed_at_utc"])
        self.assertEqual(data["pihole_daily"][0]["blocked_queries"], 1)
        self.assertEqual(data["pihole_domains"][0]["domain"], "example.com")
        self.assertEqual(data["pihole_clients"][0]["client_name"], "Laptop <private>")
        self.assertEqual([sha256(p) for p in (self.bandwidth, self.pihole)], before)

    def test_failed_second_source_rolls_back_data_and_metadata(self):
        with connect(self.db) as c:
            ensure_schema(c)
        invalid = self.root / "invalid.db"
        sqlite3.connect(invalid).close()
        with self.assertRaises(sqlite3.OperationalError):
            self.run_import("--bandwidth", self.bandwidth, "--pihole", invalid)
        with connect(self.db, read_only=True) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM speedtest_attempts").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT count(*) FROM source_metadata WHERE source_name='legacy_bandwidth'").fetchone()[0], 0)

    def test_empty_archive_imports_without_executemany_error(self):
        with sqlite3.connect(self.bandwidth) as c:
            c.execute("DELETE FROM observations")
        self.run_import("--bandwidth", self.bandwidth)
        self.assertEqual(load_data(self.db)["rows"], [])

    def test_latest_probe_batch_excludes_retired_targets_and_summaries_are_bounded(self):
        self.populate()
        with connect(self.db) as c:
            c.execute("INSERT INTO network_probes VALUES ('old','2026-01-01T18:00:00Z','dns','192.0.2.1','failure',1,'retired')")
            c.execute("INSERT INTO network_probes VALUES ('new','2026-01-01T18:05:00Z','dns','192.0.2.2','success',1,'ok')")
            for i in range(25):
                c.execute("INSERT INTO pihole_domains VALUES (?,?)", [i + 100, f"domain-{i}.example"])
                c.execute("INSERT INTO pihole_queries(query_id,timestamp_epoch,domain_id,client_id,status) VALUES (?,1767294000,?,1,2)", [i + 100, i + 100])
        data = load_data(self.db)
        self.assertEqual([r["target"] for r in data["probe_latest"]], ["192.0.2.2"])
        self.assertEqual(data["probe_daily"][0]["failures"], 1)
        self.assertEqual(len(data["pihole_domains"]), 20)
        self.assertEqual(data["pihole_domains"][0]["domain"], "example.com")

    def test_static_build_is_complete_rebuildable_and_read_only(self):
        self.populate()
        before = sha256(self.db)
        output = self.root / "viewer" / "private data"
        for _ in range(2):
            with patch.object(sys, "argv", ["export_static.py", "--db", str(self.db), "--output", str(output)]):
                self.assertEqual(export_main(), 0)
        payload = json.loads((output / "archive.json").read_text())
        self.assertEqual(payload["rows"][0][1], "2026-01-01T12:00:00")
        self.assertEqual(payload["pihole_daily"][0]["local_date"], "2026-01-01")
        self.assertIn('data-source="./private%20data/archive.json"', (output.parent / "index.html").read_text())
        self.assertTrue((output.parent / "vendor" / "plotly.min.js").is_file())
        self.assertTrue((output.parent / "vendor" / "PLOTLY-LICENSE.txt").is_file())
        manifest = json.loads((output / "manifest.json").read_text())
        self.assertEqual(len(manifest["files"]), 6)
        with duckdb.connect() as c:
            for artifact in manifest["files"].values():
                self.assertEqual(c.execute("SELECT count(*) FROM read_parquet(?)", [str(output / artifact["path"])]).fetchone()[0], artifact["rows"])
        self.assertEqual(sha256(self.db), before)


if __name__ == "__main__":
    unittest.main()

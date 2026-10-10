"""Health checks never turn missing collection or ICMP failures into uptime claims."""

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "signal"))

from db import ensure_schema  # noqa: E402
from health import collect_probes, dns_servers, freshness, latest_probes, main, probe  # noqa: E402


class SignalHealthTest(unittest.TestCase):
    def test_freshness_uses_utc_collector_attempts_even_when_failed(self):
        now = datetime(2026, 10, 10, 20, tzinfo=timezone.utc)
        with duckdb.connect(":memory:") as conn:
            ensure_schema(conn)
            self.assertEqual(freshness(conn, 1800, now)[0], "missing")
            conn.execute("""INSERT INTO speedtest_attempts
                (attempt_id,run_id,observed_at_utc,attempt_no,status,source_name)
                VALUES ('legacy','legacy',?,1,'success','legacy-bandwidth')""", [now + timedelta(days=1)])
            self.assertEqual(freshness(conn, 1800, now)[0], "missing")
            conn.execute("""INSERT INTO speedtest_attempts
                (attempt_id,run_id,observed_at_utc,attempt_no,status,source_name)
                VALUES ('live','live',?,1,'failure','collector')""", [now - timedelta(minutes=30)])
            self.assertEqual(freshness(conn, 1800, now)[0], "fresh")
            self.assertEqual(freshness(conn, 1800, now + timedelta(seconds=1))[0], "stale")
            conn.execute("UPDATE speedtest_attempts SET observed_at_utc=? WHERE attempt_id='live'",
                         [now + timedelta(minutes=5)])
            self.assertEqual(freshness(conn, 1800, now)[0], "clock_error")

    def test_dns_requires_successful_response_and_a_record(self):
        for output, code, expected in (
            (";; status: NOERROR, id: 1\nexample.com. 60 IN A 192.0.2.1", 0, "success"),
            (";; status: SERVFAIL, id: 1", 0, "failure"),
            (";; status: NXDOMAIN, id: 1", 0, "failure"),
            (";; status: NOERROR, id: 1", 0, "failure"),
            ("connection timed out", 9, "failure"),
        ):
            with self.subTest(output=output), patch("health.subprocess.run", return_value=subprocess.CompletedProcess([], code, output, "")):
                self.assertEqual(probe("dns", "192.0.2.53", ["dig"])["status"], expected)
        with patch("health.subprocess.run", side_effect=FileNotFoundError("dig unavailable")):
            self.assertEqual(probe("dns", "192.0.2.53", ["dig"])["status"], "unavailable")
        with patch("health.subprocess.run", side_effect=subprocess.TimeoutExpired(["ping"], 5)):
            self.assertEqual(probe("external", "192.0.2.1", ["ping"])["status"], "failure")

    def test_resolvers_are_unique_and_each_is_probed_separately(self):
        with TemporaryDirectory() as directory:
            resolv = Path(directory) / "resolv.conf"
            resolv.write_text("# resolvers\nnameserver 192.0.2.53\nnameserver 192.0.2.54 # backup\nnameserver 192.0.2.53\n")
            servers = dns_servers(resolv)
        self.assertEqual(servers, ["192.0.2.53", "192.0.2.54"])
        with patch("health.command_result", side_effect=[
            ("success", ";; status: NOERROR\nexample.com. 60 IN A 192.0.2.1", 1),
            ("failure", "DNS timeout", 2),
            ("success", "default via 192.0.2.254 dev eth0", 1),
            ("success", "gateway reply", 1),
            ("success", "external reply", 1),
        ]) as run:
            probes = collect_probes(servers, "", "192.0.2.1")
        self.assertEqual([(row["kind"], row["target"], row["status"]) for row in probes], [
            ("dns", "192.0.2.53", "success"), ("dns", "192.0.2.54", "failure"),
            ("gateway", "192.0.2.254", "success"), ("external", "192.0.2.1", "success")])
        self.assertEqual(len({row["observed_at_utc"] for row in probes}), 1)
        self.assertEqual(run.call_args_list[0].args[0][1], "@192.0.2.53")
        self.assertEqual(run.call_args_list[1].args[0][1], "@192.0.2.54")

    def test_missing_resolvers_and_gateway_are_not_silently_skipped(self):
        with patch("health.command_result", side_effect=[("success", "", 1), ("success", "reply", 1)]):
            probes = collect_probes([], "", "192.0.2.1")
        self.assertEqual([row["status"] for row in probes], ["unavailable", "failure", "success"])

    def test_status_reports_stale_probe_records(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "signal.duckdb"
            now = datetime.now(timezone.utc)
            with duckdb.connect(str(database)) as conn:
                ensure_schema(conn)
                conn.execute("""INSERT INTO speedtest_attempts
                    (attempt_id,run_id,observed_at_utc,attempt_no,status,source_name)
                    VALUES ('live','live',?,1,'success','collector')""", [now])
                conn.executemany("INSERT INTO network_probes VALUES (?,?,?,?,'success',1,'reply')",
                                 [(kind, now - timedelta(minutes=16), kind, "192.0.2.1")
                                  for kind in ("dns", "gateway", "external")])
            with patch.object(sys, "argv", ["health.py", "--db", str(database), "--status"]):
                self.assertEqual(main(), 1)

    def test_records_probes_and_status_is_read_only_without_network_requests(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "signal.duckdb"
            now = datetime.now(timezone.utc)
            with duckdb.connect(str(database)) as conn:
                ensure_schema(conn)
                conn.execute("""INSERT INTO speedtest_attempts
                    (attempt_id,run_id,observed_at_utc,attempt_no,status,source_name)
                    VALUES ('live','live',?,1,'success','collector')""", [now])
                conn.execute("INSERT INTO network_probes VALUES ('old',?,'dns','retired','failure',0,'old resolver')",
                             [now - timedelta(minutes=5)])
            probes = [{"probe_id": kind, "observed_at_utc": now, "kind": kind,
                       "target": "192.0.2.1", "status": "success", "elapsed_ms": 1, "detail": "reply"}
                      for kind in ("dns", "gateway", "external")]
            argv = ["health.py", "--db", str(database), "--dns-servers", "192.0.2.53"]
            with patch.object(sys, "argv", argv), patch("health.collect_probes", return_value=probes):
                self.assertEqual(main(), 0)
            with duckdb.connect(str(database), read_only=True) as conn:
                self.assertEqual(conn.execute("SELECT count(*) FROM network_probes").fetchone()[0], 4)
                self.assertEqual({row["status"] for row in latest_probes(conn)}, {"success"})
            before = hashlib.sha256(database.read_bytes()).hexdigest()
            with patch.object(sys, "argv", argv + ["--status"]), patch("health.subprocess.run") as run:
                self.assertEqual(main(), 0)
                run.assert_not_called()
            self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), before)


if __name__ == "__main__":
    unittest.main()

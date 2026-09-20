"""Checks for the cron collector wrapper."""

import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

import duckdb


ROOT = Path(__file__).resolve().parents[1]


class CollectorRunnerTest(unittest.TestCase):
    def test_status_reports_cron_log_and_latest_attempt(self):
        with TemporaryDirectory() as temp:
            temp = Path(temp)
            database = temp / "signal.duckdb"
            with duckdb.connect(str(database)) as conn:
                conn.execute("""
                    CREATE TABLE speedtest_attempts (
                        observed_at_utc TIMESTAMP,
                        observed_at_local TIMESTAMP,
                        attempt_no INTEGER,
                        status VARCHAR,
                        failure_kind VARCHAR,
                        download_mbps DOUBLE,
                        upload_mbps DOUBLE
                    )
                """)
                conn.execute("""
                    INSERT INTO speedtest_attempts VALUES
                    ('2026-09-20 15:00:00', '2026-09-20 09:00:00', 1, 'success', NULL, 800, 400)
                """)

            log = temp / "collector.log"
            log.write_text("collected run-id using 31903\n")
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            crontab = fake_bin / "crontab"
            crontab.write_text("#!/bin/sh\nprintf '%s\\n' '*/15 * * * * collector-run.sh run # signal-collector'\n")
            crontab.chmod(0o755)

            result = subprocess.run(
                [str(ROOT / "signal" / "collector-run.sh"), "status"],
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "SIGNAL_DB": str(database),
                    "SIGNAL_ENV_FILE": str(temp / "missing.env"),
                    "SIGNAL_LOG": str(log),
                    "SIGNAL_PYTHON": sys.executable,
                },
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Cron: */15 * * * * collector-run.sh run # signal-collector", result.stdout)
            self.assertIn("Latest log: collected run-id using 31903", result.stdout)
            self.assertIn("Latest attempt: 2026-09-20 09:00:00 | success | down 800.0 Mbps | up 400.0 Mbps", result.stdout)


if __name__ == "__main__":
    unittest.main()

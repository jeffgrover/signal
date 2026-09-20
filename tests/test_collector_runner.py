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
    def test_run_uses_speedtest_path_from_environment_file(self):
        with TemporaryDirectory() as temp:
            temp = Path(temp)
            speedtest = temp / "speedtest"
            speedtest.write_text("#!/bin/sh\nprintf '%s\\n' 'Speedtest by Ookla 1.2.0'\n")
            speedtest.chmod(0o755)
            python_args = temp / "python-args"
            fake_python = temp / "python"
            fake_python.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$PYTHON_ARGS\"\n")
            fake_python.chmod(0o755)
            env_file = temp / "collector.env"
            env_file.write_text(f"SIGNAL_SPEEDTEST={speedtest}\n")

            result = subprocess.run(
                [str(ROOT / "signal" / "collector-run.sh"), "run"],
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "PYTHON_ARGS": str(python_args),
                    "SIGNAL_ENV_FILE": str(env_file),
                    "SIGNAL_LOCK": str(temp / "collector.lock"),
                    "SIGNAL_LOG": str(temp / "collector.log"),
                    "SIGNAL_PYTHON": str(fake_python),
                },
                check=False,
            )
            args = python_args.read_text().splitlines()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(args[args.index("--speedtest") + 1], str(speedtest))

    def test_install_rejects_speedtest_cli_and_pins_ookla_path(self):
        with TemporaryDirectory() as temp:
            temp = Path(temp)
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            crontab_state = temp / "crontab"
            crontab_state.write_text(
                "*/15 * * * * old-runner run # signal-collector\n"
                "0 2 * * * backup\n"
            )
            crontab = fake_bin / "crontab"
            crontab.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = -l ]; then [ ! -r \"$CRONTAB_STATE\" ] || cat \"$CRONTAB_STATE\"; exit 0; fi\n"
                "if [ \"$1\" = - ]; then cat > \"$CRONTAB_STATE\"; fi\n"
            )
            crontab.chmod(0o755)
            speedtest = fake_bin / "speedtest"
            speedtest.write_text("#!/bin/sh\nprintf '%s\\n' 'speedtest-cli 2.1.3'\n")
            speedtest.chmod(0o755)
            env = {
                **os.environ,
                "CRONTAB_STATE": str(crontab_state),
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "SIGNAL_ENV_FILE": str(temp / "missing.env"),
                "SIGNAL_SPEEDTEST": str(speedtest),
            }

            result = subprocess.run(
                [str(ROOT / "signal" / "collector-run.sh"), "install"],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not the official Ookla CLI", result.stderr)
            self.assertIn("old-runner", crontab_state.read_text())

            speedtest.write_text("#!/bin/sh\nprintf '%s\\n' 'Speedtest by Ookla 1.2.0'\n")
            result = subprocess.run(
                [str(ROOT / "signal" / "collector-run.sh"), "install"],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            installed = crontab_state.read_text()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("old-runner", installed)
            self.assertIn("0 2 * * * backup", installed)
            self.assertIn(f"SIGNAL_SPEEDTEST={speedtest}", installed)
            self.assertIn("Updated:", result.stdout)

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

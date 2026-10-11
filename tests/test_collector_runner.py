"""Checks for the cron collector wrapper."""

import os
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

import duckdb


ROOT = Path(__file__).resolve().parents[1]


class CollectorRunnerTest(unittest.TestCase):
    def test_health_alerts_deduplicate_and_retry_pending_delivery(self):
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            fake_python = temp / "python"
            fake_python.write_text('#!/bin/sh\ncat "$HEALTH_REPORT"\nexit "$HEALTH_EXIT"\n')
            fake_python.chmod(0o755)
            report = temp / "report"
            notifications = temp / "notifications"
            state_file = temp / "health-state"
            env = {
                **os.environ,
                "SIGNAL_ENV_FILE": str(temp / "missing.env"),
                "SIGNAL_PYTHON": str(fake_python),
                "SIGNAL_LOCK": str(temp / "locks" / "collector.lock"),
                "SIGNAL_HEALTH_LOG": str(temp / "health.log"),
                "SIGNAL_HEALTH_STATE": str(state_file),
                "SIGNAL_NOTIFY_CMD": 'printf "%s\\n" "$SIGNAL_FAILURE_MESSAGE" >> "$NOTIFY_CAPTURE"; exit "$NOTIFY_EXIT"',
                "NOTIFY_CAPTURE": str(notifications),
                "HEALTH_REPORT": str(report),
            }
            for current, notify_exit, count in (
                ("problem:collector_stale,dns", 0, 1), ("problem:collector_stale,dns", 0, 1),
                ("healthy", 0, 2), ("healthy", 0, 2),
                ("problem:external", 1, 3), ("problem:external", 1, 4),
                ("healthy", 1, 5), ("healthy", 0, 6), ("healthy", 0, 6),
            ):
                with self.subTest(current=current, notify_exit=notify_exit, count=count):
                    report.write_text(current + "\nprivate diagnostic detail: 192.0.2.53\n")
                    result = subprocess.run(
                        [str(ROOT / "signal" / "collector-run.sh"), "check"],
                        capture_output=True, text=True,
                        env={**env, "HEALTH_EXIT": "0" if current == "healthy" else "1", "NOTIFY_EXIT": str(notify_exit)},
                        check=False,
                    )
                    self.assertEqual(result.returncode, int(current != "healthy"), result.stderr)
                    messages = notifications.read_text().splitlines()
                    self.assertEqual(len(messages), count)
                    self.assertNotIn("192.0.2.53", notifications.read_text())
                    self.assertEqual(state_file.read_text().splitlines()[0], current)
                    self.assertEqual(bool(state_file.read_text().splitlines()[1]), bool(notify_exit))
            self.assertIn("recovered; previous problems: external", messages[-1])
            self.assertIn("notification delivery failed", (temp / "health.log").read_text())

    def test_failed_run_sends_generic_alert_and_logs_delivery_failure(self):
        with TemporaryDirectory() as directory:
            temp = Path(directory)
            speedtest = temp / "speedtest"
            speedtest.write_text("#!/bin/sh\nprintf '%s\\n' 'Speedtest by Ookla 1.2.0'\n")
            speedtest.chmod(0o755)
            fake_python = temp / "python"
            fake_python.write_text("#!/bin/sh\nexit 1\n")
            fake_python.chmod(0o755)
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            logger = fake_bin / "logger"
            logger.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$LOGGER_CAPTURE"\n')
            logger.chmod(0o755)
            notification = temp / "notification"
            local_log = temp / "notification-log"
            for notify_exit in (0, 1):
                with self.subTest(notify_exit=notify_exit):
                    result = subprocess.run(
                        [str(ROOT / "signal" / "collector-run.sh"), "run"],
                        capture_output=True,
                        text=True,
                        env={
                            **os.environ,
                            "PATH": f"{fake_bin}:{os.environ['PATH']}",
                            "SIGNAL_ENV_FILE": str(temp / "missing.env"),
                            "SIGNAL_SPEEDTEST": str(speedtest),
                            "SIGNAL_PYTHON": str(fake_python),
                            "SIGNAL_LOCK": str(temp / "collector.lock"),
                            "SIGNAL_LOG": str(temp / "collector.log"),
                            "SIGNAL_RETRY_DELAY_SECONDS": "0",
                            "SIGNAL_NOTIFY_CMD": 'printf "%s" "$SIGNAL_FAILURE_MESSAGE" > "$NOTIFY_CAPTURE"; exit "$NOTIFY_EXIT"',
                            "NOTIFY_CAPTURE": str(notification),
                            "NOTIFY_EXIT": str(notify_exit),
                            "LOGGER_CAPTURE": str(local_log),
                        },
                        check=False,
                    )
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertEqual(notification.read_text(), "Signal collector failed after retry. Check collector-run.sh status on the collection machine.")
                    if notify_exit:
                        self.assertIn("notification delivery failed", result.stderr)
                        self.assertIn(str(temp / "collector.log"), result.stderr)
                        self.assertIn("failed after retry", local_log.read_text())
                    else:
                        self.assertFalse(local_log.exists())
                        self.assertEqual(result.stderr, "")

    def test_run_uses_speedtest_path_from_environment_file(self):
        with TemporaryDirectory() as temp:
            temp = Path(temp)
            speedtest = temp / "speedtest"
            speedtest.write_text("#!/bin/sh\nprintf '%s\\n' 'Speedtest by Ookla 1.2.0'\n")
            speedtest.chmod(0o755)
            python_args = temp / "python-args"
            fake_python = temp / "python"
            heartbeat_capture = temp / "heartbeat-url"
            fake_python.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$PYTHON_ARGS\"\nprintf '%s' \"$SIGNAL_HEALTHCHECK_URL\" > \"$HEARTBEAT_CAPTURE\"\n")
            fake_python.chmod(0o755)
            env_file = temp / "collector.env"
            heartbeat_url = "https://hc-ping.com/123e4567-e89b-12d3-a456-426614174000"
            env_file.write_text(f"SIGNAL_SPEEDTEST={speedtest}\nSIGNAL_HEALTHCHECK_URL={heartbeat_url}\n")

            result = subprocess.run(
                [str(ROOT / "signal" / "collector-run.sh"), "run"],
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "PYTHON_ARGS": str(python_args),
                    "HEARTBEAT_CAPTURE": str(heartbeat_capture),
                    "SIGNAL_ENV_FILE": str(env_file),
                    "SIGNAL_LOCK": str(temp / "locks" / "collector.lock"),
                    "SIGNAL_LOG": str(temp / "collector.log"),
                    "SIGNAL_PYTHON": str(fake_python),
                },
                check=False,
            )
            args = python_args.read_text().splitlines()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(args[args.index("--speedtest") + 1], str(speedtest))
            self.assertEqual(heartbeat_capture.read_text(), heartbeat_url)

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
            self.assertIn("2-59/5 * * * *", installed)
            self.assertIn("check >/dev/null 2>&1 # signal-collector-health", installed)
            result = subprocess.run([str(ROOT / "signal" / "collector-run.sh"), "install"],
                                    capture_output=True, text=True, env=env, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(crontab_state.read_text(), installed)
            result = subprocess.run([str(ROOT / "signal" / "collector-run.sh"), "uninstall"],
                                    capture_output=True, text=True, env=env, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("signal-collector", crontab_state.read_text())
            self.assertIn("0 2 * * * backup", crontab_state.read_text())

    def test_status_reports_cron_log_and_latest_attempt(self):
        with TemporaryDirectory() as temp:
            temp = Path(temp)
            database = temp / "signal.duckdb"
            now = datetime.now(timezone.utc).replace(microsecond=0)
            local = now.astimezone().replace(tzinfo=None)
            with duckdb.connect(str(database)) as conn:
                conn.execute((ROOT / "signal" / "schema.sql").read_text())
                conn.execute("""
                    INSERT INTO speedtest_attempts
                    (attempt_id,run_id,observed_at_utc,observed_at_local,attempt_no,status,download_mbps,upload_mbps,source_name)
                    VALUES ('fixture','fixture',?,?,1,'success',800,400,'collector')
                """, [now, local])
                conn.executemany("INSERT INTO network_probes VALUES (?,?,?,?,'success',1,'reply')",
                                 [(kind, now, kind, "192.0.2.1") for kind in ("dns", "gateway", "external")])

            log = temp / "collector.log"
            log.write_text("collected run-id using 31903\n")
            fake_bin = temp / "bin"
            fake_bin.mkdir()
            crontab = fake_bin / "crontab"
            crontab.write_text("#!/bin/sh\nprintf '%s\\n' '*/15 * * * * collector-run.sh run # signal-collector' '2-59/5 * * * * collector-run.sh check # signal-collector-health'\n")
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
                    "SIGNAL_HEALTHCHECK_URL": "https://hc-ping.com/123e4567-e89b-12d3-a456-426614174000",
                },
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Cron: */15 * * * * collector-run.sh run # signal-collector", result.stdout)
            self.assertIn("Latest log: collected run-id using 31903", result.stdout)
            self.assertIn(f"Latest attempt: {local} | success | down 800.0 Mbps | up 400.0 Mbps", result.stdout)
            self.assertIn("Health: healthy", result.stdout)
            self.assertIn("Healthchecks: configured", result.stdout)
            self.assertNotIn("hc-ping.com", result.stdout)


if __name__ == "__main__":
    unittest.main()

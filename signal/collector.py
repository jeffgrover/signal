#!/usr/bin/env python3
"""Collect fixed-server Ookla measurements into Signal's DuckDB."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

from db import DEFAULT_DB, connect, ensure_schema


DEFAULT_SERVER_IDS = ("31903", "12652", "2185")


class OoklaError(ValueError):
    """The command returned something other than a valid Ookla result."""


def build_command(executable: str, server_id: str) -> list[str]:
    return [executable, "--format=json", "--accept-license", "--accept-gdpr", "--server-id", server_id]


def parse_ookla(payload: str, expected_server_id: str | None = None) -> dict:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as error:
        raise OoklaError("invalid_json") from error
    if not isinstance(data, dict) or not isinstance(data.get("download"), dict) or not isinstance(data.get("upload"), dict):
        raise OoklaError("not_ookla_json")
    server = data.get("server") if isinstance(data.get("server"), dict) else {}
    server_id = str(server.get("id")) if server.get("id") is not None else None
    if expected_server_id and server_id != str(expected_server_id):
        raise OoklaError(f"server_mismatch:{server_id or 'missing'}")

    def number(value: object) -> float:
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
            raise OoklaError("invalid_metric")
        return float(value)

    packet_loss = data.get("packetLoss")
    packet_loss = 0 if packet_loss is None else packet_loss
    return {
        "server_id": server_id,
        "server_name": server.get("name"),
        "server_location": server.get("location"),
        "server_country": server.get("country"),
        "download_mbps": number(data["download"].get("bandwidth")) * 8 / 1_000_000,
        "upload_mbps": number(data["upload"].get("bandwidth")) * 8 / 1_000_000,
        "latency_ms": number((data.get("ping") or {}).get("latency", 0)),
        "jitter_ms": number((data.get("ping") or {}).get("jitter", 0)),
        "packet_loss_pct": number(packet_loss),
        "raw_json": payload,
    }


def classify_error(message: str) -> str:
    text = message.lower()
    if "timed out" in text or "timeout" in text:
        return "timeout"
    if "resolve" in text or "name or service not known" in text:
        return "dns_resolution_failed"
    if "server_mismatch" in text:
        return "server_mismatch"
    if "invalid_json" in text or "not_ookla_json" in text or "invalid_metric" in text:
        return "collector_parse_error"
    return "ookla_error"


def verify_ookla(executable: str) -> str:
    result = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=15, check=False)
    output = f"{result.stdout}\n{result.stderr}".strip()
    if result.returncode or "ookla" not in output.lower():
        raise RuntimeError(f"{executable!r} is not the official Ookla CLI")
    return output.splitlines()[0] if output else "Ookla"


def run_attempt(executable: str, server_id: str, timeout: int) -> dict:
    command = build_command(executable, server_id)
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return {"status": "failure", "failure_kind": "timeout", "error_message": "speedtest timed out", "command": command}
    if result.returncode:
        message = (result.stderr or result.stdout or f"exit status {result.returncode}").strip()
        return {"status": "failure", "failure_kind": classify_error(message), "error_message": message, "command": command}
    try:
        parsed = parse_ookla(result.stdout, server_id)
    except OoklaError as error:
        return {"status": "failure", "failure_kind": classify_error(str(error)), "error_message": str(error), "command": command}
    return {"status": "success", **parsed, "command": command}


def insert_attempt(conn, run_id: str, observed_utc: datetime, attempt_no: int, requested_server_id: str, record: dict, client_version: str, source_name: str = "collector") -> None:
    local = observed_utc.astimezone().replace(tzinfo=None)
    conn.execute(
        """INSERT INTO speedtest_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            str(uuid4()), run_id, observed_utc, local, attempt_no, requested_server_id,
            record.get("server_id"), record.get("server_name"), record.get("server_location"), record.get("server_country"),
            record["status"], record.get("failure_kind"), record.get("error_message"),
            record.get("download_mbps"), record.get("upload_mbps"), record.get("latency_ms"), record.get("jitter_ms"), record.get("packet_loss_pct"),
            client_version, " ".join(record.get("command", [])), record.get("raw_json"), source_name, None,
        ],
    )


def collect_once(conn, executable: str, server_ids: tuple[str, ...], timeout: int, client_version: str) -> str:
    run_id = str(uuid4())
    for attempt_no, server_id in enumerate(server_ids, 1):
        record = run_attempt(executable, server_id, timeout)
        insert_attempt(conn, run_id, datetime.now(timezone.utc), attempt_no, server_id, record, client_version)
        if record["status"] == "success":
            break
    conn.commit()
    return run_id


def next_boundary(now: datetime, interval_minutes: int) -> datetime:
    if interval_minutes <= 0 or 60 % interval_minutes:
        raise ValueError("interval_minutes must be a positive divisor of 60")
    base = now.replace(second=0, microsecond=0)
    return base + timedelta(minutes=interval_minutes - base.minute % interval_minutes)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--speedtest", default=os.environ.get("SIGNAL_SPEEDTEST", "speedtest"))
    parser.add_argument("--server-id", dest="server_ids", action="append", help="Pinned Ookla server ID; repeat for fallbacks")
    parser.add_argument("--interval", type=int, default=15)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    server_ids = tuple(args.server_ids or os.environ.get("SIGNAL_SERVER_IDS", ",".join(DEFAULT_SERVER_IDS)).split(","))
    if not server_ids or any(not server_id.strip() for server_id in server_ids):
        parser.error("at least one --server-id is required")
    try:
        client_version = verify_ookla(args.speedtest)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        parser.error(str(error))
    with connect(args.db) as conn:
        ensure_schema(conn)
        while True:
            run_id = collect_once(conn, args.speedtest, tuple(server_id.strip() for server_id in server_ids), args.timeout, client_version)
            print(f"collected {run_id} using {', '.join(server_ids)}", flush=True)
            if args.once:
                return 0
            time.sleep(max(0, (next_boundary(datetime.now(), args.interval) - datetime.now()).total_seconds()))


if __name__ == "__main__":
    raise SystemExit(main())

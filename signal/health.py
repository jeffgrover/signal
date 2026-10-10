#!/usr/bin/env python3
"""Check collector freshness and record independent Linux network probes."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import ipaddress
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from uuid import uuid4

import duckdb

from db import DEFAULT_DB, connect, ensure_schema


def freshness(conn, max_age_seconds: int, now: datetime | None = None) -> tuple[str, str]:
    latest = conn.execute("""SELECT max(observed_at_utc) FROM speedtest_attempts
        WHERE source_name = 'collector'""").fetchone()[0]
    if latest is None:
        return "missing", "Collector: no timestamped collector attempts recorded"
    latest = latest.replace(tzinfo=timezone.utc) if latest.tzinfo is None else latest.astimezone(timezone.utc)
    age = ((now or datetime.now(timezone.utc)) - latest).total_seconds()
    detail = f"Collector: last attempt {latest.isoformat()}, age {age / 60:.1f} min (limit {max_age_seconds / 60:g} min)"
    if age < -60:
        return "clock_error", detail
    return ("stale" if age > max_age_seconds else "fresh"), detail


def command_result(command: list[str]) -> tuple[str, str, float]:
    started = time.monotonic()
    executable = shutil.which(command[0], path=os.environ.get("PATH", os.defpath) + ":/usr/sbin:/sbin")
    command = [executable or command[0], *command[1:]]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5,
                                check=False, env={**os.environ, "LC_ALL": "C"})
        status = "success" if result.returncode == 0 else "failure"
        detail = (result.stdout + "\n" + result.stderr).strip()
    except subprocess.TimeoutExpired:
        status, detail = "failure", "Probe timed out after 5 seconds"
    except OSError as error:
        status, detail = "unavailable", str(error)
    return status, detail[:2000], (time.monotonic() - started) * 1000


def probe(kind: str, target: str, command: list[str]) -> dict:
    status, detail, elapsed = command_result(command)
    if kind == "dns" and status == "success":
        # dig exits successfully for SERVFAIL/NXDOMAIN too; require a usable A answer.
        if not re.search(r"status:\s*NOERROR\b", detail) or not re.search(r"\sIN\s+A\s+", detail):
            status = "failure"
    return {"probe_id": str(uuid4()), "observed_at_utc": datetime.now(timezone.utc),
            "kind": kind, "target": target, "status": status, "elapsed_ms": elapsed, "detail": detail}


def dns_servers(path: Path = Path("/etc/resolv.conf")) -> list[str]:
    servers = []
    for line in path.read_text().splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == "nameserver":
            address = str(ipaddress.ip_address(fields[1]))
            if address not in servers:
                servers.append(address)
    return servers


def collect_probes(servers: list[str], gateway: str, external_ip: str) -> list[dict]:
    probes = [probe("dns", server, ["dig", f"@{server}", "example.com.", "A",
              "+time=2", "+tries=1", "+noall", "+comments", "+answer"]) for server in servers]
    if not servers:
        probes.append({"probe_id": str(uuid4()), "observed_at_utc": datetime.now(timezone.utc),
                       "kind": "dns", "target": "configured-resolvers", "status": "unavailable",
                       "elapsed_ms": None, "detail": "No DNS servers configured"})
    if not gateway:
        status, detail, elapsed = command_result(["ip", "route", "show", "default"])
        for line in detail.splitlines() if status == "success" else []:
            fields = line.split()
            if "via" in fields and fields.index("via") + 1 < len(fields):
                gateway = str(ipaddress.ip_address(fields[fields.index("via") + 1]))
                break
        if not gateway:
            probes.append({"probe_id": str(uuid4()), "observed_at_utc": datetime.now(timezone.utc),
                           "kind": "gateway", "target": "default-route", "status": "unavailable" if status == "unavailable" else "failure",
                           "elapsed_ms": elapsed, "detail": detail or "No default IPv4 gateway detected"})
    if gateway:
        probes.append(probe("gateway", gateway, ["ping", "-n", "-c", "1", "-W", "2", gateway]))
    probes.append(probe("external", external_ip, ["ping", "-n", "-c", "1", "-W", "2", external_ip]))
    observed = datetime.now(timezone.utc)
    for row in probes:
        row["observed_at_utc"] = observed
    return probes


def latest_probes(conn) -> list[dict]:
    exists = conn.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='network_probes'").fetchone()[0]
    if not exists:
        return []
    result = conn.execute("""SELECT * FROM network_probes
        WHERE observed_at_utc = (SELECT max(observed_at_utc) FROM network_probes) ORDER BY kind,target""")
    columns = [column[0] for column in result.description]
    return [dict(zip(columns, row)) for row in result.fetchall()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--max-age-seconds", type=int, default=1800)
    parser.add_argument("--dns-servers", default="", help="Comma-separated IPs; default: /etc/resolv.conf")
    parser.add_argument("--gateway", default="", help="Gateway IP; default: detected IPv4 route")
    parser.add_argument("--external-ip", default="1.1.1.1")
    parser.add_argument("--status", action="store_true", help="Read-only freshness and latest probe results; no network requests")
    args = parser.parse_args()
    if args.max_age_seconds <= 0:
        parser.error("--max-age-seconds must be positive")
    try:
        if args.status:
            with connect(args.db, read_only=True) as conn:
                state, detail = freshness(conn, args.max_age_seconds)
                probes = latest_probes(conn)
        else:
            servers = list(dict.fromkeys(str(ipaddress.ip_address(value.strip())) for value in args.dns_servers.split(","))) if args.dns_servers else dns_servers()
            gateway = str(ipaddress.ip_address(args.gateway)) if args.gateway else ""
            external_ip = str(ipaddress.ip_address(args.external_ip))
            probes = collect_probes(servers, gateway, external_ip)
            with connect(args.db) as conn:
                ensure_schema(conn)
                state, detail = freshness(conn, args.max_age_seconds)
                conn.executemany("INSERT INTO network_probes VALUES (?,?,?,?,?,?,?)", [
                    [row[key] for key in ("probe_id", "observed_at_utc", "kind", "target", "status", "elapsed_ms", "detail")]
                    for row in probes])
        issues = set() if state == "fresh" else {f"collector_{state}"}
        if not {"dns", "gateway", "external"}.issubset({row["kind"] for row in probes}):
            issues.add("probes_missing")
        if probes and (datetime.now(timezone.utc) - max(row["observed_at_utc"] for row in probes)).total_seconds() > 900:
            issues.add("probes_stale")
        issues.update(row["kind"] for row in probes if row["status"] != "success")
        print("problem:" + ",".join(sorted(issues)) if issues else "healthy")
        print(detail)
        for row in probes:
            print(f"{row['kind']} {row['target']}: {row['status']} at {row['observed_at_utc'].isoformat()}")
            if row["status"] != "success":
                print(row["detail"])
        return int(bool(issues))
    except (OSError, ValueError, duckdb.Error) as error:
        print(f"Health check error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

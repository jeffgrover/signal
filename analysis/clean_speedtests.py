"""Build a reversible, log-verified SQLite dataset. Uses only Python's stdlib."""

import argparse
from bisect import bisect_left, bisect_right
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile


ROOT = Path(__file__).resolve().parents[1]
RULE_VERSION = 1


def number(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def close(left, right):
    return number(left) and number(right) and math.isclose(
        left, right, rel_tol=1e-8, abs_tol=1e-6
    )


def normalize(payload):
    """Translate the two known CLI schemas; never fill absent metrics with zero."""
    down, up, ping = (payload.get(k) for k in ("download", "upload", "ping"))
    if isinstance(down, dict) and isinstance(up, dict):
        client = "ookla"
        down, up = down.get("bandwidth"), up.get("bandwidth")
        down = down * 8 / 1_000_000 if number(down) else None
        up = up * 8 / 1_000_000 if number(up) else None
        ping = ping.get("latency") if isinstance(ping, dict) else None
    else:
        client = "python_speedtest_cli"
        down = down / 1_000_000 if number(down) else None
        up = up / 1_000_000 if number(up) else None
    server = payload.get("server") or {}
    if not isinstance(server, dict):
        server = {}
    return {
        "download_mbps": down,
        "upload_mbps": up,
        "latency_ms": ping if number(ping) else None,
        "client": client,
        "server_id": str(server["id"]) if server.get("id") is not None else None,
        "server_name": server.get("sponsor") if client == "python_speedtest_cli" else server.get("name"),
        "server_location": server.get("name") if client == "python_speedtest_cli" else server.get("location"),
        "server_country": server.get("country"),
    }


def read_log(path):
    entries = []
    skipped = 0
    with path.open(encoding="utf-8", errors="replace") as lines:
        for line_number, line in enumerate(lines, 1):
            if "Raw speedtest output: " not in line:
                continue
            try:
                stamp = datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f")
                payload = json.loads(line.split("Raw speedtest output: ", 1)[1])
                if not isinstance(payload, dict):
                    raise ValueError("result is not an object")
                entries.append((stamp, line_number, normalize(payload)))
            except (ValueError, TypeError):
                skipped += 1
    entries.sort(key=lambda entry: entry[0])
    return entries, [entry[0] for entry in entries], skipped


def classify_error(error):
    """Classify observed failure stages, not ISP blame or verified outages."""
    if not error:
        return None, None
    text = error.lower()
    if "unsupported operand type(s) for /: 'dict' and 'int'" in text:
        return "collector_parse_error", None
    if any(term in text for term in ("couldn't resolve host", "name resolution", "hostnotfound")):
        return "dns_resolution_failed", "dns"
    if "no route to host" in text:
        return "no_route_to_host", "routing"
    if "network is unreachable" in text or "network unreachable" in text:
        return "network_unreachable", "routing"
    if "timed out" in text or "timeout" in text:
        return "request_timeout", "timeout"
    if "unable to connect to servers to test latency" in text:
        return "server_selection_failed", "uncertain"
    if "http error 502" in text:
        return "test_service_http_502", "remote_service"
    return "unclassified_error", "uncertain"


def clean_row(row, entries, times):
    stamp = datetime.fromisoformat(row["timestamp"])
    candidates = entries[
        bisect_left(times, stamp - timedelta(seconds=3)):bisect_right(times, stamp)
    ]
    kind, evidence = classify_error(row["error"])
    result = dict.fromkeys((
        "download_mbps", "upload_mbps", "latency_ms", "bandwidth_scale",
        "client", "server_id", "server_name", "server_location", "server_country",
        "source_log_line", "source_log_timestamp",
    ))
    result.update(
        source_rowid=row["source_rowid"], failure_kind=kind,
        connection_evidence=evidence, log_match="none",
        bandwidth_status="not_available" if kind else "unverified",
        latency_status="not_available" if kind else "unverified",
    )
    matches = []
    for log_stamp, log_line, data in candidates:
        if not all(number(data[k]) for k in ("download_mbps", "upload_mbps")):
            continue
        if kind == "collector_parse_error":
            matches.append((log_stamp, log_line, data, None))
        elif not kind:
            # Both directions and ping must agree, including in the repeated DST hour.
            if not close(row["ping"], data["latency_ms"]):
                continue
            for scale in (1, 8):
                if all(number(row[k]) and close(row[k] * scale, data[target])
                       for k, target in (("download", "download_mbps"), ("upload", "upload_mbps"))):
                    matches.append((log_stamp, log_line, data, scale))
                    break
    if len(matches) != 1:
        if matches:
            result["log_match"] = "ambiguous"
        return result
    log_stamp, log_line, data, scale = matches[0]
    result.update(data)
    result.update(
        log_match="unique", bandwidth_scale=scale,
        source_log_line=log_line, source_log_timestamp=log_stamp.isoformat(),
        bandwidth_status=("recovered_from_log" if kind else "corrected_x8" if scale == 8 else "verified"),
        latency_status="valid" if number(data["latency_ms"]) else "missing_in_log",
    )
    # speedtest-cli 2.1.3 adds 3600 seconds per failed probe, then divides by 6
    # and multiplies by 1000. One failed probe contributes 600,000 ms.
    if data["client"] == "python_speedtest_cli" and number(data["latency_ms"]) and data["latency_ms"] >= 600_000:
        result["latency_ms"] = None
        result["latency_status"] = "invalid_cli_failure_sentinel"
    return result


SCHEMA = """
CREATE TABLE measurement_quality (
    source_rowid INTEGER PRIMARY KEY,
    download_mbps REAL, upload_mbps REAL, latency_ms REAL,
    bandwidth_scale REAL, bandwidth_status TEXT NOT NULL, latency_status TEXT NOT NULL,
    failure_kind TEXT, connection_evidence TEXT, log_match TEXT NOT NULL,
    client TEXT, server_id TEXT, server_name TEXT, server_location TEXT, server_country TEXT,
    source_log_line INTEGER, source_log_timestamp TEXT
);
CREATE TABLE analysis_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX analysis_timestamp ON speedtests(timestamp);
CREATE VIEW observations AS
SELECT s.timestamp AS timestamp_local, s.download AS original_download,
       s.upload AS original_upload, s.ping AS original_ping, s.error AS original_error, q.*
FROM speedtests s JOIN measurement_quality q ON q.source_rowid = s.rowid;
CREATE VIEW throughput_samples AS SELECT * FROM observations
WHERE bandwidth_status IN ('verified', 'corrected_x8', 'recovered_from_log');
CREATE VIEW latency_samples AS SELECT * FROM observations WHERE latency_status = 'valid';
CREATE VIEW connection_errors AS SELECT * FROM observations
WHERE connection_evidence IN ('dns', 'routing', 'timeout');
CREATE VIEW uncertain_test_failures AS SELECT * FROM observations
WHERE failure_kind = 'server_selection_failed';
CREATE VIEW test_failures AS SELECT * FROM observations
WHERE original_error IS NOT NULL AND original_error != '';
"""


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(source, log, output):
    source, log, output = (Path(p).resolve() for p in (source, log, output))
    if output in (source, log) or output.exists():
        raise FileExistsError("Choose a new output path; existing files are never overwritten.")
    hashes = {"source_sha256": fingerprint(source), "log_sha256": fingerprint(log)}
    entries, times, skipped = read_log(log)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as staging:
        destination = Path(staging) / "analysis.db"
        src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
        db = sqlite3.connect(destination)
        try:
            src.backup(db)
            src.close()
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT rowid AS source_rowid, * FROM speedtests ORDER BY rowid").fetchall()
            db.executescript(SCHEMA)
            results = [clean_row(row, entries, times) for row in rows]
            columns = list(results[0]) if results else []
            if columns:
                db.executemany(
                    f"INSERT INTO measurement_quality ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                    ([row[key] for key in columns] for row in results),
                )
            summary = {
                "rows_preserved": len(rows),
                "bandwidth_status": dict(Counter(r["bandwidth_status"] for r in results)),
                "latency_status": dict(Counter(r["latency_status"] for r in results)),
                "failure_kind": dict(Counter(r["failure_kind"] for r in results if r["failure_kind"])),
                "log_match": dict(Counter(r["log_match"] for r in results)),
                "skipped_raw_log_entries": skipped,
            }
            metadata = {
                **hashes, "source_path": str(source), "log_path": str(log),
                "rule_version": RULE_VERSION, "created_utc": datetime.now(timezone.utc).isoformat(),
                "summary": summary,
                "timestamps": "Original local timestamps; no UTC conversion or gap interpolation.",
                "error_interpretation": "Failure stage is evidence, not proof of ISP outage or downtime.",
            }
            db.executemany("INSERT INTO analysis_metadata VALUES (?, ?)",
                           ((key, json.dumps(value)) for key, value in metadata.items()))
            db.commit()
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert db.execute("SELECT count(*) FROM observations").fetchone()[0] == len(rows)
        finally:
            src.close()
            db.close()
        if fingerprint(source) != hashes["source_sha256"] or fingerprint(log) != hashes["log_sha256"]:
            raise RuntimeError("An input changed during analysis; no output published.")
        os.link(destination, output)  # Atomic publication, refusing an existing destination.
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "speedtest-review-2026-09-19.db")
    parser.add_argument("--log", type=Path, default=ROOT / "production-review/speedtest.log")
    parser.add_argument("--output", type=Path, default=ROOT / "analysis/data/speedtests-clean.db")
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.log, args.output), indent=2))

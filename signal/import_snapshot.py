#!/usr/bin/env python3
"""Import immutable SQLite snapshots into Signal's DuckDB."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3

from db import DEFAULT_DB, connect, ensure_schema


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def decode(value):
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def import_bandwidth(conn, path: Path) -> int:
    source = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    source.row_factory = sqlite3.Row
    try:
        rows = source.execute("""
            SELECT source_rowid, timestamp_local, download_mbps, upload_mbps, latency_ms,
                   failure_kind, client, server_id, server_name, server_location, server_country,
                   original_error, source_log_line
            FROM observations ORDER BY source_rowid
        """).fetchall()
    except sqlite3.Error as error:
        source.close()
        raise SystemExit(f"{path} is not a cleaned bandwidth snapshot: {error}") from error
    values = []
    for row in rows:
        success = row["download_mbps"] is not None and row["upload_mbps"] is not None
        source_rowid = int(row["source_rowid"])
        values.append([
            f"legacy-speedtest-{source_rowid}", f"legacy-speedtest-{source_rowid}", None, row["timestamp_local"], 1,
            row["server_id"], row["server_id"], row["server_name"], row["server_location"], row["server_country"],
            "success" if success else "failure", row["failure_kind"], row["original_error"],
            row["download_mbps"], row["upload_mbps"], row["latency_ms"], None, None,
            row["client"] or "historical", None, None, "legacy-bandwidth", source_rowid,
        ])
    try:
        import pandas as pd
    except ImportError as error:
        raise SystemExit("Bandwidth import needs pandas; install the repository requirements") from error
    frame = pd.DataFrame.from_records(values, columns=[
        "attempt_id", "run_id", "observed_at_utc", "observed_at_local", "attempt_no",
        "requested_server_id", "server_id", "server_name", "server_location", "server_country",
        "status", "failure_kind", "error_message", "download_mbps", "upload_mbps", "latency_ms",
        "jitter_ms", "packet_loss_pct", "client_version", "command", "raw_json", "source_name", "source_rowid",
    ])
    conn.register("_bandwidth_rows", frame)
    conn.execute("INSERT OR IGNORE INTO speedtest_attempts SELECT * FROM _bandwidth_rows")
    conn.unregister("_bandwidth_rows")
    source.close()
    return len(values)


def insert_batches(conn, source, table: str, columns: list[str], query: str, key: str | None = None, batch_size: int = 100_000) -> int:
    cursor = source.execute(query)
    total = 0
    try:
        import pandas as pd
    except ImportError as error:
        raise SystemExit("Pi-hole import needs pandas; install the repository requirements") from error
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        decoded = [tuple(decode(value) for value in row) for row in rows]
        frame = pd.DataFrame.from_records(decoded, columns=columns)
        conn.register("_signal_batch", frame)
        modifier = "OR IGNORE" if key else "OR REPLACE"
        conn.execute(f"INSERT {modifier} INTO {table} SELECT * FROM _signal_batch")
        conn.unregister("_signal_batch")
        total += len(decoded)
        if total % 1_000_000 < batch_size:
            print(f"  {table}: {total:,}", flush=True)
    return total


def import_pihole(conn, path: Path) -> int:
    source = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    source.text_factory = bytes
    try:
        insert_batches(conn, source, "pihole_domains", ["domain_id", "domain"], "SELECT id,domain FROM domain_by_id")
        insert_batches(conn, source, "pihole_clients", ["client_id", "ip", "name"], "SELECT id,ip,name FROM client_by_id")
        insert_batches(conn, source, "pihole_forwarders", ["forward_id", "forwarder"], "SELECT id,forward FROM forward_by_id")
        insert_batches(
            conn, source, "pihole_network",
            ["network_id", "hwaddr", "interface", "first_seen_epoch", "last_query_epoch", "num_queries", "mac_vendor", "aliasclient_id"],
            "SELECT id,hwaddr,interface,firstSeen,lastQuery,numQueries,macVendor,aliasclient_id FROM network",
        )
        insert_batches(
            conn, source, "pihole_network_addresses",
            ["network_id", "ip", "last_seen_epoch", "name", "name_updated_epoch"],
            "SELECT network_id,ip,lastSeen,name,nameUpdated FROM network_addresses",
        )
        query_count = insert_batches(
            conn, source, "pihole_queries",
            ["query_id", "timestamp_epoch", "query_type", "status", "domain_id", "client_id", "forward_id", "reply_type", "reply_time_s", "dnssec", "list_id", "ede"],
            """SELECT id,timestamp,type,status,domain,client,forward,reply_type,reply_time,dnssec,list_id,ede
               FROM query_storage ORDER BY id""", key="query_id",
        )
    finally:
        source.close()
    return query_count


def record_source(conn, name: str, path: Path, rows: int, notes: str) -> None:
    conn.execute("""INSERT OR REPLACE INTO source_metadata VALUES (?,?,?,?,?,?)""", [
        name, str(path.resolve()), sha256(path), rows, datetime.now(timezone.utc), notes,
    ])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--bandwidth", type=Path, help="Cleaned SQLite bandwidth snapshot")
    parser.add_argument("--pihole", type=Path, help="Read-only Pi-hole FTL SQLite backup")
    args = parser.parse_args()
    if not args.bandwidth and not args.pihole:
        parser.error("provide --bandwidth and/or --pihole")
    with connect(args.db) as conn:
        ensure_schema(conn)
        if args.bandwidth:
            print(f"Importing bandwidth archive {args.bandwidth}…", flush=True)
            count = import_bandwidth(conn, args.bandwidth)
            record_source(conn, "legacy_bandwidth", args.bandwidth, count, "Cleaned immutable speedtest archive")
            print(f"  imported {count:,} bandwidth rows", flush=True)
        if args.pihole:
            print(f"Importing Pi-hole archive {args.pihole}…", flush=True)
            count = import_pihole(conn, args.pihole)
            record_source(conn, "pihole_ftl", args.pihole, count, "Immutable FTL database backup")
            print(f"  imported {count:,} Pi-hole queries", flush=True)
        conn.execute("CHECKPOINT")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

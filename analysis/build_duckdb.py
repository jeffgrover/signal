"""Build the local analytical DuckDB from immutable SQLite source snapshots.

The SQLite files remain the provenance copies. This script creates a normalized,
read-only-friendly working database and never writes to either source.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3
import sys

try:
    import duckdb
    import pandas as pd
except ImportError as error:  # pragma: no cover - exercised by the CLI only
    raise SystemExit("DuckDB and pandas are required: python3 -m pip install duckdb pandas") from error


ROOT = Path(__file__).resolve().parents[1]
BANDWIDTH_DB = ROOT / "analysis/data/speedtests-clean.db"
PIHOLE_DB = ROOT / "pihole-FTL-review-2026-09-19.db"
OUTPUT_DB = ROOT / "analysis/data/signal.duckdb"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sqlite_readonly(path: Path) -> sqlite3.Connection:
    # immutable prevents SQLite from creating -wal/-shm files while inspecting a snapshot.
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)


def create_schema(db):
    db.execute("""
        CREATE TABLE source_metadata(
            source_name VARCHAR PRIMARY KEY, path VARCHAR, sha256 VARCHAR NOT NULL,
            row_count BIGINT, captured_utc TIMESTAMPTZ, notes VARCHAR
        )
    """)
    db.execute("""
        CREATE TABLE bandwidth_observations(
            source_rowid BIGINT, timestamp_local TIMESTAMP, download_mbps DOUBLE,
            upload_mbps DOUBLE, latency_ms DOUBLE, failure_kind VARCHAR,
            bandwidth_status VARCHAR, latency_status VARCHAR, client VARCHAR,
            server_id VARCHAR, original_download DOUBLE, original_upload DOUBLE,
            original_ping DOUBLE, original_error VARCHAR, source_log_line BIGINT
        )
    """)
    db.execute("""
        CREATE TABLE bandwidth_servers(
            server_id VARCHAR PRIMARY KEY, server_name VARCHAR, server_location VARCHAR,
            server_country VARCHAR
        )
    """)
    db.execute("""
        CREATE TABLE pihole_queries(
            query_id BIGINT, timestamp_epoch DOUBLE, query_type INTEGER, status INTEGER,
            domain_id INTEGER, client_id INTEGER, forward_id INTEGER,
            additional_info_id INTEGER, reply_type INTEGER, reply_time_s DOUBLE,
            dnssec INTEGER, list_id INTEGER, ede INTEGER
        )
    """)
    db.execute("CREATE TABLE pihole_domains(domain_id INTEGER PRIMARY KEY, domain VARCHAR)")
    db.execute("CREATE TABLE pihole_clients(client_id INTEGER PRIMARY KEY, ip VARCHAR, name VARCHAR)")
    db.execute("CREATE TABLE pihole_forwarders(forward_id INTEGER PRIMARY KEY, forward VARCHAR)")
    db.execute("""CREATE TABLE pihole_network(
        network_id INTEGER PRIMARY KEY, hwaddr VARCHAR, interface VARCHAR,
        first_seen_epoch BIGINT, last_query_epoch BIGINT, num_queries BIGINT,
        mac_vendor VARCHAR, aliasclient_id INTEGER
    )""")
    db.execute("""CREATE TABLE pihole_network_addresses(
        network_id INTEGER, ip VARCHAR PRIMARY KEY, last_seen_epoch BIGINT,
        name VARCHAR, name_updated_epoch BIGINT
    )""")
    db.execute("CREATE TABLE pihole_additional_info(additional_info_id INTEGER PRIMARY KEY, info_type INTEGER, content VARCHAR)")
    db.execute("CREATE TABLE pihole_alias_clients(aliasclient_id INTEGER PRIMARY KEY, name VARCHAR, comment VARCHAR)")
    db.execute("""CREATE TABLE pihole_messages(
        message_id BIGINT, timestamp_epoch BIGINT, message_type VARCHAR,
        message VARCHAR, blob1 VARCHAR, blob2 VARCHAR, blob3 VARCHAR,
        blob4 VARCHAR, blob5 VARCHAR
    )""")
    db.execute("""
        CREATE TABLE pihole_query_type_codes(code INTEGER PRIMARY KEY, label VARCHAR, description VARCHAR)
    """)
    db.execute("""
        CREATE TABLE pihole_status_codes(code INTEGER PRIMARY KEY, label VARCHAR, class VARCHAR, description VARCHAR)
    """)
    db.execute("""
        CREATE VIEW pihole_queries_enriched AS
        SELECT q.query_id, q.timestamp_epoch,
          timezone('America/Denver', to_timestamp(q.timestamp_epoch)) AS timestamp_local,
          q.query_type, coalesce(t.label, 'TYPE' || q.query_type::VARCHAR) AS query_type_label,
          q.status, coalesce(s.label, 'Unknown status ' || q.status::VARCHAR) AS status_label,
          coalesce(s.class, 'unknown') AS status_class,
          coalesce(s.class = 'blocked', false) AS is_blocked,
          coalesce(s.class = 'allowed_forwarded', false) AS is_forwarded,
          d.domain, c.ip AS client_ip, c.name AS client_name,
          f.forward AS forwarder, q.additional_info_id, q.reply_type, q.reply_time_s,
          q.dnssec, q.list_id, q.ede, n.mac_vendor, n.hwaddr
        FROM pihole_queries q
        LEFT JOIN pihole_domains d ON d.domain_id = q.domain_id
        LEFT JOIN pihole_clients c ON c.client_id = q.client_id
        LEFT JOIN pihole_forwarders f ON f.forward_id = q.forward_id
        LEFT JOIN pihole_query_type_codes t ON t.code = q.query_type
        LEFT JOIN pihole_status_codes s ON s.code = q.status
        LEFT JOIN pihole_network_addresses na ON na.ip = c.ip
        LEFT JOIN pihole_network n ON n.network_id = na.network_id
    """)
    db.execute("""
        CREATE VIEW pihole_daily AS
        SELECT date_trunc('day', timestamp_local)::DATE AS local_date,
          count(*) AS total_queries, count(DISTINCT client_ip) AS clients,
          count(DISTINCT domain) AS domains, count(*) FILTER (WHERE is_blocked) AS blocked_queries,
          count(*) FILTER (WHERE is_forwarded) AS forwarded_queries,
          count(*) FILTER (WHERE status_class = 'allowed_cached') AS cached_queries
        FROM pihole_queries_enriched GROUP BY 1 ORDER BY 1
    """)
    db.execute("""
        CREATE VIEW bandwidth_dns_failure_windows AS
        SELECT b.source_rowid, b.timestamp_local, b.failure_kind,
          count(p.query_id) AS nearby_queries,
          count(DISTINCT p.client_ip) AS nearby_clients,
          count(DISTINCT p.domain) AS nearby_domains,
          count(*) FILTER (WHERE p.status_class = 'blocked') AS nearby_blocked,
          count(*) FILTER (WHERE p.status_class = 'allowed_forwarded') AS nearby_forwarded,
          count(*) FILTER (WHERE p.status_class = 'allowed_cached') AS nearby_cached,
          string_agg(DISTINCT p.forwarder, ', ' ORDER BY p.forwarder)
            FILTER (WHERE p.forwarder IS NOT NULL) AS forwarders
        FROM bandwidth_observations b
        LEFT JOIN pihole_queries_enriched p
          ON b.failure_kind = 'dns_resolution_failed'
          AND p.timestamp_local BETWEEN b.timestamp_local - INTERVAL 2 MINUTE
                                    AND b.timestamp_local + INTERVAL 2 MINUTE
        WHERE b.failure_kind = 'dns_resolution_failed'
        GROUP BY b.source_rowid, b.timestamp_local, b.failure_kind
        ORDER BY b.timestamp_local
    """)


def insert_sqlite_table(db, source, target, columns, query, batch_size=100_000, progress_label=None):
    cursor = source.execute(query)
    total = 0
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        # FTL normally stores UTF-8 text, but a small number of live domain labels
        # contain invalid bytes. Keep the row and make the replacement explicit.
        rows = [tuple(value.decode('utf-8', 'replace') if isinstance(value, bytes) else value for value in row) for row in rows]
        # Registering a DataFrame lets DuckDB ingest a vectorized batch instead of
        # crossing the Python/SQL boundary once per row.
        frame = pd.DataFrame.from_records(rows, columns=columns)
        db.register('_sqlite_batch', frame)
        db.execute(f"INSERT INTO {target} SELECT * FROM _sqlite_batch")
        db.unregister('_sqlite_batch')
        total += len(rows)
        if progress_label and total % 1_000_000 < batch_size:
            print(f"  {progress_label}: {total:,}", flush=True)
    return total


def build(output: Path, bandwidth: Path, pihole: Path):
    for path in (bandwidth, pihole):
        if not path.is_file():
            raise FileNotFoundError(path)
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite {output}; remove it or choose --output")
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".tmp")
    if temp.exists():
        raise FileExistsError(f"Refusing to overwrite {temp}")
    bw = sqlite_readonly(bandwidth)
    ph = sqlite_readonly(pihole)
    ph.text_factory = bytes
    db = duckdb.connect(str(temp))
    db.execute("PRAGMA threads=4")
    db.execute("PRAGMA enable_progress_bar=false")
    create_schema(db)
    captured = datetime.now(timezone.utc)
    try:
        print("Importing bandwidth archive…", flush=True)
        bw_rows = insert_sqlite_table(
            db, bw, "bandwidth_observations",
            ["source_rowid", "timestamp_local", "download_mbps", "upload_mbps", "latency_ms",
             "failure_kind", "bandwidth_status", "latency_status", "client", "server_id",
             "original_download", "original_upload", "original_ping", "original_error", "source_log_line"],
            "SELECT source_rowid,timestamp_local,download_mbps,upload_mbps,latency_ms,failure_kind,bandwidth_status,latency_status,client,server_id,original_download,original_upload,original_ping,original_error,source_log_line FROM observations ORDER BY source_rowid",
            progress_label="bandwidth rows")
        insert_sqlite_table(db, bw, "bandwidth_servers", ["server_id", "server_name", "server_location", "server_country"],
            "SELECT DISTINCT server_id,server_name,server_location,server_country FROM observations WHERE server_id IS NOT NULL", progress_label="bandwidth servers")
        print("Importing Pi-hole dimensions…", flush=True)
        dims = [
            ("pihole_domains", ["domain_id", "domain"], "SELECT id,domain FROM domain_by_id"),
            ("pihole_clients", ["client_id", "ip", "name"], "SELECT id,ip,name FROM client_by_id"),
            ("pihole_forwarders", ["forward_id", "forward"], "SELECT id,forward FROM forward_by_id"),
            ("pihole_network", ["network_id", "hwaddr", "interface", "first_seen_epoch", "last_query_epoch", "num_queries", "mac_vendor", "aliasclient_id"], "SELECT id,hwaddr,interface,firstSeen,lastQuery,numQueries,macVendor,aliasclient_id FROM network"),
            ("pihole_network_addresses", ["network_id", "ip", "last_seen_epoch", "name", "name_updated_epoch"], "SELECT network_id,ip,lastSeen,name,nameUpdated FROM network_addresses"),
            ("pihole_additional_info", ["additional_info_id", "info_type", "content"], "SELECT id,type,CAST(content AS VARCHAR) FROM addinfo_by_id"),
            ("pihole_alias_clients", ["aliasclient_id", "name", "comment"], "SELECT id,name,comment FROM aliasclient"),
            ("pihole_messages", ["message_id", "timestamp_epoch", "message_type", "message", "blob1", "blob2", "blob3", "blob4", "blob5"], "SELECT id,timestamp,type,message,blob1,blob2,blob3,blob4,blob5 FROM message"),
        ]
        for target, columns, query in dims:
            insert_sqlite_table(db, ph, target, columns, query)
        db.executemany("INSERT INTO pihole_query_type_codes VALUES (?,?,?)", [
            (1,'A','IPv4 address'),(2,'AAAA','IPv6 address'),(3,'ANY','Any record'),(4,'SRV','Service'),
            (5,'SOA','Start of authority'),(6,'PTR','Reverse lookup'),(7,'TXT','Text'),(8,'NAPTR','Naming authority pointer'),
            (9,'MX','Mail exchange'),(10,'DS','Delegation signer'),(11,'RRSIG','DNSSEC signature'),(12,'DNSKEY','DNSSEC key'),
            (13,'NS','Name server'),(14,'OTHER','Other'),(15,'SVCB','Service binding'),(16,'HTTPS','HTTPS service binding')])
        db.executemany("INSERT INTO pihole_status_codes VALUES (?,?,?,?)", [
            (0,'Unknown','unknown','Not yet known'),(1,'Blocked · gravity','blocked','Domain in gravity database'),
            (2,'Allowed · forwarded','allowed_forwarded','Forwarded upstream'),(3,'Allowed · cache','allowed_cached','Replied from cache'),
            (4,'Blocked · regex','blocked','Regex denylist'),(5,'Blocked · exact','blocked','Exact denylist'),
            (6,'Blocked · upstream IP','blocked','Blocked by upstream blocking page'),(7,'Blocked · upstream null','blocked','Blocked by upstream null response'),
            (8,'Blocked · upstream NXDOMAIN','blocked','Blocked by upstream NXDOMAIN'),(9,'Blocked · deep CNAME gravity','blocked','Gravity block during deep CNAME inspection'),
            (10,'Blocked · deep CNAME regex','blocked','Regex block during deep CNAME inspection'),(11,'Blocked · deep CNAME exact','blocked','Exact block during deep CNAME inspection'),
            (12,'Allowed · retried','allowed_other','Retried query'),(13,'Allowed · retried ignored','allowed_other','Retried but ignored'),
            (14,'Allowed · already forwarded','allowed_forwarded','Already forwarded'),(15,'Blocked · database busy','blocked','Blocked while database was busy'),
            (16,'Blocked · special domain','blocked','Special domain, such as canary/private relay'),(17,'Allowed · stale cache','allowed_cached','Replied from stale cache'),
            (18,'Blocked · upstream EDE','blocked','Blocked by upstream extended DNS error')])
        print("Importing Pi-hole query history…", flush=True)
        ph_rows = insert_sqlite_table(db, ph, "pihole_queries",
            ["query_id", "timestamp_epoch", "query_type", "status", "domain_id", "client_id", "forward_id", "additional_info_id", "reply_type", "reply_time_s", "dnssec", "list_id", "ede"],
            "SELECT id,timestamp,type,status,domain,client,forward,additional_info,reply_type,reply_time,dnssec,list_id,ede FROM query_storage ORDER BY id",
            progress_label="Pi-hole queries")
        db.execute("INSERT INTO source_metadata VALUES (?,?,?,?,?,?)", ('bandwidth',str(bandwidth),sha256(bandwidth),bw_rows,captured,'Cleaned speedtest archive; original source remains SQLite'))
        db.execute("INSERT INTO source_metadata VALUES (?,?,?,?,?,?)", ('pihole_ftl',str(pihole),sha256(pihole),ph_rows,captured,'Online SQLite backup of an active FTL database'))
        db.execute("CHECKPOINT")
        db.close()
        bw.close(); ph.close()
        temp.replace(output)
    except Exception:
        db.close()
        bw.close(); ph.close()
        temp.unlink(missing_ok=True)
        raise
    print(f"Wrote {output} ({output.stat().st_size:,} bytes)", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bandwidth", type=Path, default=BANDWIDTH_DB)
    parser.add_argument("--pihole", type=Path, default=PIHOLE_DB)
    parser.add_argument("--output", type=Path, default=OUTPUT_DB)
    args = parser.parse_args()
    try:
        build(args.output, args.bandwidth, args.pihole)
    except (FileNotFoundError, FileExistsError, OSError) as error:
        raise SystemExit(str(error))

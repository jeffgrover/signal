"""Serve a local, read-only SQLite or Signal DuckDB network archive."""

import argparse
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent
DATABASE = ROOT / "analysis/data/speedtests-clean.db"
FIELDS = [
    "source_rowid", "timestamp_local", "download_mbps", "upload_mbps", "latency_ms",
    "failure_kind", "bandwidth_status", "latency_status", "client", "server_id",
    "original_download", "original_upload", "original_ping", "original_error", "source_log_line",
]
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.mjs": ("app.mjs", "text/javascript; charset=utf-8"),
    "/analytics.mjs": ("analytics.mjs", "text/javascript; charset=utf-8"),
    "/vendor/plotly.min.js": ("vendor/plotly.min.js", "text/javascript; charset=utf-8"),
}


def load_data(path):
    if Path(path).suffix == ".duckdb":
        import duckdb

        try:
            with duckdb.connect(str(path), read_only=True) as conn:
                return signal_data(conn)
        except duckdb.Error as error:
            raise ValueError(str(error)) from error
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = conn.execute(f"SELECT {','.join(FIELDS)} FROM observations ORDER BY source_rowid").fetchall()
        servers = conn.execute("""SELECT DISTINCT server_id, server_name, server_location, server_country
                                  FROM observations WHERE server_id IS NOT NULL""").fetchall()
        meta = {key: json.loads(value) for key, value in conn.execute(
            "SELECT key,value FROM analysis_metadata WHERE key IN ('created_utc','source_sha256','rule_version')"
        )}
        return {"fields": FIELDS, "rows": rows, "servers": servers, "meta": meta}
    finally:
        conn.close()


def signal_data(conn):
    """Present canonical measurements using the existing chart contract."""
    rows = conn.execute("""
        SELECT s.source_rowid, s.observed_at_local, s.download_mbps, s.upload_mbps,
               s.latency_ms, s.failure_kind,
               COALESCE(q.bandwidth_status, CASE WHEN s.status='success' THEN 'measured' ELSE 'not_available' END),
               COALESCE(q.latency_status, CASE WHEN s.latency_ms IS NULL THEN 'not_available' ELSE 'valid' END),
               CASE WHEN lower(s.client_version) LIKE '%ookla%' THEN 'ookla'
                    WHEN lower(s.client_version) LIKE '%python%' THEN 'python_speedtest_cli'
                    ELSE s.client_version END,
               s.server_id, q.original_download, q.original_upload, q.original_ping,
               s.error_message, q.source_log_line, s.observed_at_utc, s.source_name, s.attempt_id
        FROM speedtest_attempts s LEFT JOIN speedtest_quality q USING(attempt_id)
        ORDER BY CASE WHEN s.source_name='legacy-bandwidth' THEN 0 ELSE 1 END,
                 s.source_rowid, s.observed_at_utc, s.attempt_no, s.attempt_id
    """).fetchall()
    servers = conn.execute("""SELECT DISTINCT server_id, server_name, server_location, server_country
                              FROM speedtest_attempts WHERE server_id IS NOT NULL""").fetchall()

    def records(query):
        cursor = conn.execute(query)
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]

    return {
        "fields": FIELDS + ["observed_at_utc", "source_name", "attempt_id"], "rows": rows, "servers": servers,
        "meta": {"storage": "signal_duckdb", "created_utc": conn.execute("SELECT now()").fetchone()[0]},
        "pihole_daily": records("SELECT * FROM pihole_daily ORDER BY local_date"),
        "pihole_domains": records("""SELECT * FROM pihole_domain_monthly
            QUALIFY row_number() OVER (PARTITION BY month ORDER BY total_queries DESC, domain) <= 20
            ORDER BY month DESC, total_queries DESC, domain"""),
        "pihole_clients": records("""SELECT * FROM pihole_client_monthly
            QUALIFY row_number() OVER (PARTITION BY month ORDER BY total_queries DESC, client_ip) <= 20
            ORDER BY month DESC, total_queries DESC, client_ip"""),
        "probe_latest": records("""SELECT observed_at_utc, kind, target, status, elapsed_ms, detail
            FROM network_probes WHERE observed_at_utc=(SELECT max(observed_at_utc) FROM network_probes)
            ORDER BY kind,target"""),
        "probe_daily": records("""SELECT cast(timezone('America/Denver',observed_at_utc) AS DATE) AS local_date,
            kind, count(*) AS attempts, count(*) FILTER(WHERE status='failure') AS failures,
            count(*) FILTER(WHERE status='unavailable') AS unavailable
            FROM network_probes GROUP BY 1,2 ORDER BY 1,2"""),
    }


def make_handler(data):
    # ponytail: one immutable snapshot in memory; query SQLite by range if the archive reaches millions of rows.
    raw = json.dumps(data, separators=(",", ":"), allow_nan=False, default=lambda value: value.isoformat()).encode()
    compressed = gzip.compress(raw)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/api/data":
                encoded = "gzip" in self.headers.get("Accept-Encoding", "")
                self.respond(compressed if encoded else raw, "application/json", encoded)
            elif path in STATIC:
                filename, content_type = STATIC[path]
                self.respond((ROOT / "web" / filename).read_bytes(), content_type)
            elif path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
            else:
                self.send_error(404)

        def respond(self, body, content_type, encoded=False):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'")
            if encoded:
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            self.end_headers()
            self.wfile.write(body)

    return Handler


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DATABASE)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    try:
        data = load_data(args.db)
    except (sqlite3.Error, ValueError, ImportError) as error:
        parser.error(f"Cannot read the database: {error}. See README.md for setup and import commands.")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(data))
    print(f"Network archive: http://127.0.0.1:{server.server_port} • {len(data['rows']):,} observations", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

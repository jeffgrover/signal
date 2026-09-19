"""Serve the local, read-only network archive. No Python dependencies required."""

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


def make_handler(data):
    # ponytail: one immutable snapshot in memory; query SQLite by range if the archive reaches millions of rows.
    raw = json.dumps(data, separators=(",", ":"), allow_nan=False).encode()
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
    except sqlite3.Error as error:
        parser.error(f"Cannot read the cleaned database: {error}. Run analysis/clean_speedtests.py first.")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(data))
    print(f"Network archive: http://127.0.0.1:{server.server_port} • {len(data['rows']):,} observations", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

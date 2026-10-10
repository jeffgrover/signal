#!/usr/bin/env python3
"""Export DuckDB tables for a serverless/static Signal viewer."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path
import shutil
import sys
from urllib.parse import quote

from db import DEFAULT_DB, connect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dashboard import signal_data


def sql_path(path: Path) -> str:
    return "'" + str(path.resolve()).replace("'", "''") + "'"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "static" / "data",
                        help="Data directory; viewer assets are written to its parent")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with connect(args.db, read_only=True) as conn:
        conn.execute("BEGIN TRANSACTION")
        exports = {
            "speedtest_attempts": "SELECT * FROM speedtest_attempts ORDER BY observed_at_utc NULLS LAST, attempt_id",
            "speedtest_quality": "SELECT * FROM speedtest_quality ORDER BY attempt_id",
            "pihole_daily": "SELECT * FROM pihole_daily ORDER BY local_date",
            "pihole_domain_monthly": "SELECT * FROM pihole_domain_monthly ORDER BY month, total_queries DESC",
            "pihole_client_monthly": "SELECT * FROM pihole_client_monthly ORDER BY month, total_queries DESC",
            "network_probes": "SELECT * FROM network_probes ORDER BY observed_at_utc",
        }
        manifest = {"generated_utc": datetime.now(timezone.utc).isoformat(), "database": str(args.db.resolve()), "files": {}}
        for name, query in exports.items():
            target = args.output / f"{name}.parquet"
            conn.execute(f"COPY ({query}) TO {sql_path(target)} (FORMAT PARQUET, COMPRESSION ZSTD)")
            count = conn.execute(f"SELECT count(*) FROM read_parquet({sql_path(target)})").fetchone()[0]
            manifest["files"][name] = {"path": target.name, "rows": count}
        archive = json.dumps(
            signal_data(conn), separators=(",", ":"), allow_nan=False, default=lambda value: value.isoformat()
        ) + "\n"
        conn.execute("COMMIT")
    viewer = args.output.parent
    for name in ("app.css", "app.mjs", "analytics.mjs"):
        shutil.copyfile(ROOT / "web" / name, viewer / name)
    shutil.copytree(ROOT / "web" / "vendor", viewer / "vendor", dirs_exist_ok=True)
    data_url = escape(f"./{quote(args.output.name)}/archive.json", quote=True)
    html = (ROOT / "web" / "index.html").read_text().replace('<body>', f'<body data-source="{data_url}">')
    (viewer / "index.html").write_text(html)
    # Replace the browser's entry point only after the full snapshot is ready.
    archive_tmp = args.output / "archive.json.tmp"
    archive_tmp.write_text(archive)
    archive_tmp.replace(args.output / "archive.json")
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Wrote private viewer {viewer} ({len(manifest['files'])} parquet files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

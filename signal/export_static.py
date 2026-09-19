#!/usr/bin/env python3
"""Export DuckDB tables for a serverless/static Signal viewer."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from db import DEFAULT_DB, connect


def sql_path(path: Path) -> str:
    return "'" + str(path.resolve()).replace("'", "''") + "'"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "static" / "data")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with connect(args.db, read_only=True) as conn:
        exports = {
            "speedtest_attempts": "SELECT * FROM speedtest_attempts ORDER BY observed_at_utc NULLS LAST, attempt_id",
            "pihole_daily": "SELECT * FROM pihole_daily ORDER BY local_date",
            "pihole_domain_monthly": "SELECT * FROM pihole_domain_monthly ORDER BY month, total_queries DESC",
            "pihole_client_monthly": "SELECT * FROM pihole_client_monthly ORDER BY month, total_queries DESC",
        }
        manifest = {"generated_utc": datetime.now(timezone.utc).isoformat(), "database": str(args.db.resolve()), "files": {}}
        for name, query in exports.items():
            target = args.output / f"{name}.parquet"
            conn.execute(f"COPY ({query}) TO {sql_path(target)} (FORMAT PARQUET, COMPRESSION ZSTD)")
            count = conn.execute(f"SELECT count(*) FROM read_parquet({sql_path(target)})").fetchone()[0]
            manifest["files"][name] = {"path": target.name, "rows": count}
        (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Wrote {args.output} ({len(manifest['files'])} parquet files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

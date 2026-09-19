# Signal

Signal is the next-generation home-network archive. The collector records one
official Ookla result at a time, using pinned server IDs and retaining every
failed primary or fallback attempt. DuckDB is the canonical working database.

The current repository contains historical analysis under `analysis/`. Signal
is the clean forward path; it does not rewrite the historical SQLite sources.

## Collection

Install the official Ookla CLI as `speedtest`, then run a one-shot check:

```sh
python3 signal/collector.py --once
```

The default order is:

1. Google Fiber Salt Lake City (`31903`)
2. XMission Salt Lake City (`12652`)
3. SUMOFIBER Salt Lake City (`2185`)

Override the order with repeated `--server-id` flags. A failed primary is kept
as an attempt and the collector tries the next server. The command refuses the
Python `speedtest-cli` package; supporting two incompatible JSON schemas was a
major source of historical ambiguity.

Run continuously every fifteen minutes with:

```sh
python3 signal/collector.py
```

The canonical database is `signal/data/signal.duckdb`, which is ignored by Git.
It contains raw JSON, server identity, requested server, attempt order,
metrics, failure stage, and source metadata.

## Import and static build

Import the existing cleaned bandwidth archive and the read-only Pi-hole backup
into the same DuckDB file:

```sh
python3 signal/import_snapshot.py \
  --bandwidth analysis/data/speedtests-clean.db \
  --pihole pihole-FTL-review-2026-09-19.db
```

Build-time export produces Parquet artifacts for speedtest attempts, daily
Pi-hole volume, and monthly domain/client summaries suitable for a static
viewer or GitHub Pages:

```sh
python3 signal/export_static.py
```

This follows the useful part of `arcade-road-trip`: collection and imports run
where the data lives, while the deployed viewer is a static artifact generated
from DuckDB. No live database or API belongs in the public web path.

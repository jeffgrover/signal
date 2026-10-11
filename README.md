# Signal — home network archive explorer

A private dashboard for home-network speedtest history, Pi-hole activity, and
connectivity probes. The Pi collects into DuckDB; a static snapshot runs on the
laptop. Historical corrections remain traceable to the original records.

## Private laptop viewer

Install `signal/requirements.txt` in a virtual environment, then build from a
consistent copy of the Pi's canonical database:

```sh
.venv/bin/python signal/export_static.py --db signal/data/signal.duckdb
python3 -m http.server 8765 --bind 127.0.0.1 --directory signal/static
```

Open **http://127.0.0.1:8765**. The build includes the existing speed charts,
historical correction details, daily Pi-hole volume, monthly top domains and
clients, and separate DNS/gateway/external probe results. It needs no database
or Python packages to serve. Assets are bundled; charts make no external requests.

The viewer is a snapshot, not a live connection to the Pi. Copy a newer database,
rerun the export, and reload to refresh. Keep `signal/data/` and `signal/static/`
private: both are ignored by Git, and domain/client summaries are browsing
history. Do not upload this build to public hosting. See
[signal/README.md](signal/README.md#import-and-static-build) for safe backup,
import, transfer, and refresh commands.

## Legacy SQLite viewer

```sh
python3 dashboard.py
```

Open **http://127.0.0.1:8765**. Python 3.10+ is sufficient; there are no Python packages to install. Plotly.js 2.35.2 is bundled locally, so the dashboard makes no external requests. The server listens only on loopback and opens SQLite read-only. Restart it to load a newer snapshot.

The default database is `analysis/data/speedtests-clean.db`, generated from the private snapshot and production log. Those inputs and generated databases are excluded from Git. See [analysis/README.md](analysis/README.md) for the correction rules and reproduction commands. To use another compatible analysis database or port:

```sh
python3 dashboard.py --db analysis/data/rebuilt.db --port 8766
```

## Explore

- **Overview and history:** full-history sample means, daily download/upload means and an optional trailing seven-calendar-day mean. Hover, zoom, or click a day for the underlying tests.
- **Failure evidence:** DNS, routing, timeout, uncertain server-selection, test-service and recovered collector failures. Click a category to focus its timeline and episodes. The schedule chart compares outcomes by recorded minute of the hour.
- **Daily and weekly patterns:** hourly mean and average daily minimum/maximum, weekday/weekend comparisons, and a weekday/hour heatmap.
- **Latency and endpoints:** daily median/p95 latency after excluding known invalid placeholders; compare client/server combinations to reduce measurement-method confounding.
- **Investigate next:** suggested fixed-endpoint comparisons, separate Pi-hole/gateway/external-IP probes, and collector heartbeat/supervision. Recording gaps and the data-quality accounting remain visible.

Date filters apply to every analytical view. **Client and endpoint filters apply to performance measurements only**: most failed tests have no client/server metadata. Recommendations and the correction audit describe the full archive. “Last 30/90 days” ends on the snapshot's last recorded date, not today's date.

“Export samples” downloads the performance-filtered corrected observations as CSV, including original values, source row IDs, quality flags and source log lines. It does not export failure-only rows; those remain available in the day detail and the analysis database. Chart toolbar camera buttons export PNG images.

## What the charts mean

- Overall and daily bandwidth means weight each measured test equally. Seven-day means pool the tests in the trailing seven days within the selected date range. Unobserved days remain gaps, including in the smoothed view.
- Intraday low/mean/high first calculates each date/hour's minimum, mean and maximum, then averages these across dates, giving each observed date equal weight. The band is the average daily range, not a confidence interval or an all-time extreme. The heatmap uses the same equal-date weighting.
- Failure episodes join consecutive observations of the same type, splitting at a different outcome, a gap over 30 minutes, or a backward clock jump. Their first-to-last observed spans are **not outage durations**. Missing collection and failed-test percentages cannot establish uptime.
- Original local timestamps are displayed unchanged. Historical rows have no stored timezone offset; forward attempts also retain UTC. The UI uses a virtual clock to prevent the browser from silently shifting local times. Recording gaps over two hours are flagged; one-hour daylight-saving changes are not labeled gaps.
- Valid low bandwidth and high latency remain. DNS failure does not identify an ISP failure or which of the two Pi-holes was involved. Both endpoint choice and the switch between Ookla and Python speedtest-cli can affect comparisons.

The September 19, 2026 archive contains **30,944 observations**, **21,436 measured throughput results** and **20,327 latency results without known invalid markers**. Corrected full-history sample means are **673.3 Mbps down / 622.7 Mbps up**. A later throughput decline, clustered DNS errors, and frequent half-hour server-selection failures are useful investigation targets, with the limitations above.

## Verify

```sh
python3 -m unittest discover -s analysis -p 'test_*.py'
python3 -m unittest discover -s tests -p 'test_*.py'
node --test tests/test_analytics.mjs
```

The checks cover correction provenance, preservation of original records, read-only serving and file boundaries, missing/zero readings, date/client/server filtering, daily weighting, episode boundaries and the scheduling profile. Node is needed only for the JavaScript tests. Browser checks cover the loaded archive, filters, drill-downs, empty states and responsive layout.

The bundled chart library retains its upstream header and [MIT license](web/vendor/PLOTLY-LICENSE.txt).

## Signal forward collector and unified storage

The forward path now lives under [signal/](signal/). It standardizes on the
official Ookla CLI, pins three historically stable Salt Lake-area servers, and
retains failed primary and fallback attempts in DuckDB. It refuses the Python
`speedtest-cli` JSON format so future trend lines remain comparable. The laptop's
working copy can receive the historical archive and Pi-hole FTL backup while
the Pi continues collecting:

On the collection machine, create the environment and install DuckDB before
running the collector:

```sh
cd /path/to/signal
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r signal/requirements.txt
```

The cron wrapper automatically uses `.venv/bin/python`; use
`SIGNAL_PYTHON=/path/to/signal/.venv/bin/python` in
`~/.config/signal/collector.env` to override it.

`signal/collector-run.sh install` installs fifteen-minute collection and
five-minute health checks. The latter record separate DNS/gateway/external
probes, detect stale collector data, and send transition/recovery alerts through
the configured notification hook. `signal/collector-run.sh status` reports
both jobs, recent data/probes, and pending delivery. See [signal/README.md](signal/README.md)
for dependencies, alert setup, and overrides. Optional Healthchecks.io heartbeats
are sent only after recorded collection, so cloud alerts can detect a dead host,
stopped cron, or loss of internet access without exposing a home-network endpoint.
See [off-host setup](signal/README.md#off-host-heartbeat-healthchecksio).

On the laptop, after transferring a consistent collector database copy and
the immutable archives:

```sh
.venv/bin/python signal/import_snapshot.py --db signal/data/signal.duckdb \
  --bandwidth analysis/data/speedtests-clean.db \
  --pihole /path/to/pihole-FTL-backup.db
.venv/bin/python signal/export_static.py --db signal/data/signal.duckdb
```

The build produces a complete private static viewer plus six Parquet artifacts
for analysis, including historical quality flags and network probes. Collection
happens on the home machine; bulk imports and builds run on the laptop's copy.
An eleven-million-query import stalled our 4 GB collection/DNS host; do not run
large imports on that live host. See the transfer workflow linked above.
For a read-only dynamic preview, `dashboard.py --db signal/data/signal.duckdb`
also supports the canonical schema (DuckDB must be installed).

## Pi-hole exploration and historical storage

Pi-hole FTL backups are imported as read-only inputs and are intentionally
ignored because they contain private query history. `analysis/build_duckdb.py`
remains the reproducible historical-analysis builder;
`signal/import_snapshot.py` is the forward canonical importer. The existing
SQLite dashboard remains available alongside the unified DuckDB/static viewer.

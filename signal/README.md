# Signal

Signal is the next-generation home-network archive. The collector records one
official Ookla result at a time, using pinned server IDs and retaining every
failed primary or fallback attempt. DuckDB is the canonical working database.

The current repository contains historical analysis under `analysis/`. Signal
is the clean forward path; it does not rewrite the historical SQLite sources.

## Collection

Create the virtual environment once on the collection machine, then install
the small runtime dependency set:

```sh
cd /path/to/signal
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r signal/requirements.txt
```

The runner automatically uses `.venv/bin/python` when it exists. Set
`SIGNAL_PYTHON` in `~/.config/signal/collector.env` only if the environment is
somewhere else.

Install the official Ookla CLI as `speedtest`, then run a one-shot check:

```sh
python3 signal/collector.py --once
```

The default order is:

1. Google Fiber Salt Lake City (`31903`)
2. XMission Salt Lake City (`12652`)
3. SUMOFIBER Salt Lake City (`2185`)

Override the order with repeated `--server-id` flags. A server-selection or
server-identity failure is kept as an attempt and the collector tries the next
server; DNS and configuration failures stop the run so they are not amplified
by immediate retries. The command refuses the Python `speedtest-cli` package;
supporting two incompatible JSON schemas was a major source of historical
ambiguity.

For a manual run with timeout, locking, one retry, and failure logging:

```sh
signal/collector-run.sh run
```

Install the wrapper as a fifteen-minute collector job and a five-minute health
job (offset to minutes 2, 7, 12, and so on):

```sh
signal/collector-run.sh install
```

Installation resolves `SIGNAL_SPEEDTEST` or `speedtest` from the current
`PATH`, verifies that it is the official Ookla CLI, and records its absolute
path in the cron entry. Re-run `install` to update an existing Signal entry.

Check both installed cron entries, latest collector log/attempt, freshness,
latest probe results, and any pending health notification:

```sh
signal/collector-run.sh status
```

Remove it with `signal/collector-run.sh uninstall`. Set
`SIGNAL_NOTIFY_CMD` in `~/.config/signal/collector.env` to a notification
command; the failed-run message is available to that command as
`$SIGNAL_FAILURE_MESSAGE`.

For ntfy, subscribe to an unguessable topic at `https://ntfy.sh/app`, allow
desktop notifications, and enable background notifications in Settings. Add
this line to `~/.config/signal/collector.env`, replacing `YOUR_RANDOM_TOPIC`:

```sh
SIGNAL_NOTIFY_CMD='/usr/bin/curl --fail --silent --show-error --max-time 15 --header "Title: Signal collector alert" --header "Tags: warning" --data-binary "$SIGNAL_FAILURE_MESSAGE" "https://ntfy.sh/YOUR_RANDOM_TOPIC" >/dev/null'
```

Use the absolute path reported by `command -v curl` if it differs. No ntfy
client installation is needed. The alert contains no hostname, local path,
metrics, or browsing history. Free unreserved topics are not access-controlled:
anyone who knows the topic can read or publish messages. Keep the topic in the
machine's private environment file, not in Git. A failed notification falls
back to local logging.

Test the configured hook without interrupting collection:

```sh
. "$HOME/.config/signal/collector.env"
SIGNAL_FAILURE_MESSAGE='Signal notification test: the collector alert hook is connected.' bash -c "$SIGNAL_NOTIFY_CMD"
```

Chrome and Firefox must remain running for desktop delivery; the ntfy tab can
be closed when background notifications are enabled. Alerts cannot be sent
while the collection machine has no internet connection.

The wrapper uses `flock` to prevent overlapping runs, `timeout` to detect a
stuck Ookla process, and one retry before notifying. Run it directly instead
of starting the collector's continuous loop when cron owns scheduling.

The continuous loop remains available for development:

```sh
python3 signal/collector.py
```

The canonical database is `signal/data/signal.duckdb`, which is ignored by Git.
It contains raw JSON, server identity, requested server, attempt order,
metrics, failure stage, and source metadata.

## Freshness and network health

Run the same health check used by cron:

```sh
signal/collector-run.sh check
```

The check records each configured resolver's response to an `example.com` A
query, one ICMP probe to the detected IPv4 gateway, and one to `1.1.1.1`.
It stores timestamped results and diagnostic output in `network_probes`, separate
from speedtests, and logs checks in `signal/data/health.log`. On Debian/Pi OS,
the native tools come from `dnsutils`, `iputils-ping`, and `iproute2`; no new
Python dependency is required. System `sbin` directories are also searched
under cron's limited PATH. Probe commands have a five-second timeout and the
whole check has a sixty-second timeout, with a five-second kill grace period.

A collector attempt more than 30 minutes old triggers a stale-data alert.
Failed measurements still count as a collector heartbeat; imported historical
rows do not. The read-only `status` command also flags probe records more than
15 minutes old. Future collector timestamps over one minute ahead flag a clock
error rather than being treated as fresh forever.

Health alerts use the same `SIGNAL_NOTIFY_CMD` hook. They are sent when the
problem categories change and when health recovers, not every five minutes.
Undelivered messages are retained in a private state file and retried; if the
network recovers before delivery, a recovery notice includes the previous
problem categories. Neither probe addresses nor diagnostic output are sent
to the notification service. Repeated failed collector runs still use their
existing per-run alerts.

Override discovery in `~/.config/signal/collector.env` if needed:

```sh
SIGNAL_DNS_SERVERS=192.0.2.53,192.0.2.54
SIGNAL_GATEWAY=192.0.2.1
SIGNAL_EXTERNAL_IP=1.1.1.1
SIGNAL_STALE_SECONDS=1800
```

The check shares the collector's lock and skips a tick when collection is
busy. Missing tools/configuration are reported explicitly, not silently
counted as working probes. DNS answers can be cached, and an ICMP failure
can mean ping is blocked: neither establishes an ISP outage or uptime.

<!-- shortcut: checks run on the collection host; add an off-host heartbeat monitor to detect a dead host or stopped cron daemon. -->
A local cron check can detect a stopped collector schedule while cron itself
still works. It cannot notify while the Pi is off, cron is stopped, or the
internet connection is completely down. Detecting those silences promptly
requires an off-host heartbeat monitor; local probe history and pending
recovery notices still preserve evidence once operation resumes.

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

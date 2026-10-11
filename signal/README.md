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

A local cron check can detect a stopped collector schedule while cron itself
still works. It cannot notify while the Pi is off, cron is stopped, or the
internet connection is completely down. Detecting those silences promptly
requires an off-host heartbeat monitor; local probe history and pending
recovery notices still preserve evidence once operation resumes.

## Off-host heartbeat (Healthchecks.io)

Create one check at [Healthchecks.io](https://healthchecks.io/) named
`Signal collector heartbeat`, with a **15-minute period** and a **15-minute
grace time**. This alerts about 30 minutes after the last recorded run. Configure
the check's ntfy integration for your existing private topic; an email integration
can be kept as a backup. The free plan's 20-check limit counts monitored jobs,
not individual heartbeat requests.

Add the check's UUID ping URL to the collection machine's private
`~/.config/signal/collector.env` (keep that file mode `600`):

```sh
SIGNAL_HEALTHCHECK_URL='https://hc-ping.com/YOUR_CHECK_UUID'
```

No cron changes are needed. The wrapper passes the URL to the collector, which
sends an empty HTTPS POST only **after a new attempt has been stored and
committed**. Both successful and failed speedtests count as activity; existing
alerts still report measurement failures. A database failure, missing CLI,
stuck run, or stopped schedule does not send a heartbeat. There is no independent
"I'm alive" job that could hide a broken collector.

The URL must be the check's `https://hc-ping.com/<uuid>` success URL, without
`/start`, `/fail`, or query parameters. It is a private capability: anyone who
knows it can spoof activity. Do not put it in Git or chat. For direct development
runs of `collector.py`, export the variable first; the shell wrapper handles it
automatically. Unconfigured collection behaves as before.

Heartbeat delivery has a ten-second network timeout, validates Healthchecks'
`OK` response, and logs only a generic success/failure message. A delivery failure
does not fail or repeat an otherwise successful measurement; the next recorded
run tries again. `collector-run.sh status` reports whether it is configured;
the collector log and Healthchecks dashboard show delivery. Verify a real
scheduled run appears in the dashboard, then use the integration's test button
to check notification delivery without stopping production collection.

No metrics, DNS history, hostnames, or diagnostic logs are uploaded. Healthchecks
still sees the request's source IP and arrival time, and stores your notification
destination. Its missing-heartbeat alert detects silence, not whether the cause
was the collector, power, DNS, or internet access. Receiving the cloud alert
during a home-network outage requires another working connection, such as a
phone's mobile data; the offline laptop cannot receive it until it reconnects.

## Import and static build

Make a SQLite online backup of the live Pi-hole database before importing it;
do not copy a live SQLite file with `cp` or open it with `immutable=1`. The backup
API includes committed WAL data without changing the live source. On the Pi:

```sh
umask 077
sudo python3 - <<'PY'
import os
from pathlib import Path
import sqlite3

target = Path('signal/data/pihole-snapshot.db').resolve()
if target.exists():
    raise SystemExit('Backup already exists; choose a new filename')
target.touch(mode=0o600, exist_ok=False)
with sqlite3.connect('file:/etc/pihole/pihole-FTL.db?mode=ro', uri=True) as source:
    with sqlite3.connect(target) as backup:
        source.backup(backup)
os.chmod(target, 0o600)
os.chown(target, int(os.environ['SUDO_UID']), int(os.environ['SUDO_GID']))
PY
```

Keep bulk imports off the live collection/DNS machine. Our eleven-million-query
import stalled a 4 GB host; the successful laptop import peaked at 4.65 GiB
resident memory. The exact cause was not established by the Pi's kernel logs.
A small Python batch does not bound the memory needed by the whole transaction.
Transfer the immutable archives and a consistent collector copy, then import
into a working copy on the laptop.

To transfer a consistent DuckDB copy, checkpoint and copy it under the collector
lock on the Pi. Choose an unused snapshot filename:

```sh
flock -w 5 /tmp/signal-collector.lock .venv/bin/python - <<'PY'
from pathlib import Path
import shutil
import duckdb

source = Path('signal/data/signal.duckdb')
target = Path('signal/data/viewer-snapshot.duckdb')
if target.exists():
    raise SystemExit('Snapshot already exists; choose a new filename')
target.touch(mode=0o600, exist_ok=False)
with duckdb.connect(str(source)) as conn:
    conn.execute('CHECKPOINT')
    shutil.copyfile(source, target)
target.chmod(0o600)
PY
```

On the laptop, copy the offline files using SCP (substitute your host and path).
Choose unused local filenames on each refresh; keep the collector snapshot
unchanged and import into a separate working copy:

```sh
umask 077
mkdir -p signal/data
scp USER@HOST:/path/to/signal/signal/data/viewer-snapshot.duckdb signal/data/viewer-snapshot.duckdb
scp USER@HOST:/path/to/signal/signal/data/pihole-snapshot.db signal/data/pihole-snapshot.db
scp USER@HOST:/path/to/signal/analysis/data/speedtests-clean.db signal/data/bandwidth-snapshot.db
test ! -e signal/data/viewer-working.duckdb && cp -p signal/data/viewer-snapshot.duckdb signal/data/viewer-working.duckdb
.venv/bin/python signal/import_snapshot.py --db signal/data/viewer-working.duckdb \
  --bandwidth signal/data/bandwidth-snapshot.db \
  --pihole signal/data/pihole-snapshot.db
.venv/bin/python signal/export_static.py --db signal/data/viewer-working.duckdb
python3 -m http.server 8765 --bind 127.0.0.1 --directory signal/static
```

The import is atomic, keeps source hashes and historical quality flags/original
values, and does not modify the SQLite inputs. Rerunning the same archive is
idempotent. IDs belong to one historical archive and one FTL database lineage:
do not merge unrelated databases or a reset FTL database using overlapping IDs.
Never copy or import over the Pi's active database. If importing on a collection
machine for a small development fixture, hold `/tmp/signal-collector.lock` with
`flock`; collection and health checks skip ticks while that lock is held.

Open `http://127.0.0.1:8765`. Export opens DuckDB read-only and generates HTML,
bundled chart assets, `data/archive.json`, a manifest, and six compressed Parquet
files: attempts, historical quality, daily Pi-hole volume, monthly domains,
monthly clients, and network probes. The JSON includes the top 20 domains and
clients per month, not every raw DNS query. `--output /path/to/viewer/data` places
data there and viewer assets in `/path/to/viewer`.

Daily DNS/probe charts follow date filters; monthly tables use whole overlapping
months. Client/endpoint filters affect speed measurements only. Latest probe
results are from the last completed batch in the snapshot, not live status.
Historical local timestamps are preserved; forward attempts also retain UTC.

<!-- shortcut: refresh is a manual snapshot/transfer/export; automate only when the viewer's permanent host is chosen. -->
Repeat snapshot, transfer, local import, export, and browser reload for fresh
data. Nothing is automatically synchronized. The static directory is movable
to another private host later, without adding a database server or redesigning
the charts.
Both `signal/data/` and `signal/static/` are ignored by Git. **Do not publish
these artifacts publicly:** domain/client summaries contain private history.

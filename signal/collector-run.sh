#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${SIGNAL_ENV_FILE:-$HOME/.config/signal/collector.env}"
[[ -r "$ENV_FILE" ]] && source "$ENV_FILE"

if [[ -n "${SIGNAL_PYTHON:-}" ]]; then
    PYTHON="$SIGNAL_PYTHON"
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
    PYTHON="$ROOT/.venv/bin/python"
else
    PYTHON="python3"
fi
COLLECTOR="$ROOT/signal/collector.py"
DB="${SIGNAL_DB:-$ROOT/signal/data/signal.duckdb}"
LOG="${SIGNAL_LOG:-$ROOT/signal/data/collector.log}"
LOCK="${SIGNAL_LOCK:-${XDG_RUNTIME_DIR:-/tmp}/signal-collector.lock}"
TIMEOUT_SECONDS="${SIGNAL_TIMEOUT_SECONDS:-300}"
RETRY_DELAY_SECONDS="${SIGNAL_RETRY_DELAY_SECONDS:-30}"
TAG="# signal-collector"

resolve_speedtest() {
    local executable version
    executable="$(command -v "${SIGNAL_SPEEDTEST:-speedtest}" 2>/dev/null || true)"
    if [[ -z "$executable" ]]; then
        printf 'Official Ookla CLI not found; install speedtest or set SIGNAL_SPEEDTEST.\n' >&2
        return 1
    fi
    if ! version="$("$executable" --version 2>&1)"; then
        printf 'Unable to run %s --version.\n' "$executable" >&2
        return 1
    fi
    if [[ "${version,,}" != *ookla* ]]; then
        printf '%s is not the official Ookla CLI.\n' "$executable" >&2
        return 1
    fi
    printf '%s\n' "$executable"
}

run_once() {
    local speedtest
    mkdir -p "$(dirname -- "$LOG")" "$(dirname -- "$LOCK")"
    speedtest="$(resolve_speedtest 2>>"$LOG")" || return 1
    "$(command -v timeout)" "$TIMEOUT_SECONDS" "$PYTHON" "$COLLECTOR" --db "$DB" --speedtest "$speedtest" --once >>"$LOG" 2>&1
}

notify_failure() {
    local message="Signal collector failed after retry on $(hostname) at $(date -Is). See $LOG"
    if [[ -n "${SIGNAL_NOTIFY_CMD:-}" ]]; then
        SIGNAL_FAILURE_MESSAGE="$message" bash -c "$SIGNAL_NOTIFY_CMD" || true
    else
        logger -t signal-collector -- "$message" 2>/dev/null || true
        printf '%s\n' "$message" >&2
    fi
}

run() {
    local flock_bin
    flock_bin="$(command -v flock)"
    exec 9>"$LOCK"
    "$flock_bin" -n 9 || exit 0

    if run_once; then
        return 0
    fi
    sleep "$RETRY_DELAY_SECONDS"
    if run_once; then
        return 0
    fi
    notify_failure
    return 1
}

status() {
    local cron_entry exit_code=0
    cron_entry="$(crontab -l 2>/dev/null | grep -F "$TAG" || true)"
    printf 'Cron: %s\n' "${cron_entry:-not installed}"
    [[ -n "$cron_entry" ]] || exit_code=1

    if [[ -s "$LOG" ]]; then
        printf 'Latest log: '
        tail -n 1 "$LOG"
    else
        printf 'Latest log: none at %s\n' "$LOG"
        exit_code=1
    fi

    if [[ -r "$DB" ]]; then
        printf 'Latest attempt: '
        if ! "$PYTHON" - "$DB" <<'PY'
import sys

import duckdb


row = duckdb.connect(sys.argv[1], read_only=True).execute("""
    SELECT observed_at_local, status, failure_kind, download_mbps, upload_mbps
    FROM speedtest_attempts
    ORDER BY observed_at_utc DESC NULLS LAST, attempt_no DESC
    LIMIT 1
""").fetchone()
if row is None:
    print("none")
else:
    observed, result, failure, download, upload = row
    metrics = "" if download is None else f" | down {download:.1f} Mbps | up {upload:.1f} Mbps"
    failure = "" if failure is None else f" | {failure}"
    print(f"{observed} | {result}{failure}{metrics}")
PY
        then
            exit_code=1
        fi
    else
        printf 'Latest attempt: no database at %s\n' "$DB"
        exit_code=1
    fi
    return "$exit_code"
}

install_cron() {
    local action cron_line current speedtest
    speedtest="$(resolve_speedtest)" || return 1
    printf -v cron_line '*/15 * * * * SIGNAL_SPEEDTEST=%q %q run %s' "$speedtest" "$ROOT/signal/collector-run.sh" "$TAG"
    current="$(crontab -l 2>/dev/null || true)"
    if printf '%s\n' "$current" | grep -Fxq "$cron_line"; then
        printf 'Signal cron job is already installed.\n'
        return 0
    fi
    action="Installed"
    if printf '%s\n' "$current" | grep -Fq "$TAG"; then
        action="Updated"
    fi
    {
        printf '%s\n' "$current" | awk -v tag="$TAG" 'index($0, tag) == 0'
        printf '%s\n' "$cron_line"
    } | crontab -
    printf '%s: %s\n' "$action" "$cron_line"
}

uninstall_cron() {
    local current
    current="$(crontab -l 2>/dev/null || true)"
    [[ -z "$current" ]] && return 0
    printf '%s\n' "$current" | awk -v tag="$TAG" 'index($0, tag) == 0' | crontab -
    printf 'Removed Signal cron job.\n'
}

case "${1:-run}" in
    run) run ;;
    status) status ;;
    install) install_cron ;;
    uninstall) uninstall_cron ;;
    *)
        printf 'Usage: %s [run|status|install|uninstall]\n' "$0" >&2
        exit 2
        ;;
esac

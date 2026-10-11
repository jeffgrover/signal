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
HEALTH_LOG="${SIGNAL_HEALTH_LOG:-$ROOT/signal/data/health.log}"
HEALTH_STATE="${SIGNAL_HEALTH_STATE:-$DB.health-state}"
STALE_SECONDS="${SIGNAL_STALE_SECONDS:-1800}"
TAG="# signal-collector"

resolve_speedtest() {
    local executable version
    executable="$(command -v "${SIGNAL_SPEEDTEST:-speedtest}" 2>/dev/null || true)"
    if [[ -z "$executable" ]]; then
        printf 'Official Ookla CLI not found; install speedtest or set SIGNAL_SPEEDTEST.\n' >&2
        return 1
    fi
    if ! version="$(timeout --kill-after=5 15 "$executable" --version 2>&1)"; then
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
    SIGNAL_HEALTHCHECK_URL="${SIGNAL_HEALTHCHECK_URL:-}" "$(command -v timeout)" --kill-after=10 "$TIMEOUT_SECONDS" "$PYTHON" "$COLLECTOR" --db "$DB" --speedtest "$speedtest" --once >>"$LOG" 2>&1
}

notify_failure() {
    local message="${1:-Signal collector failed after retry. Check collector-run.sh status on the collection machine.}"
    local log="${2:-$LOG}" delivery_status=0
    if [[ -n "${SIGNAL_NOTIFY_CMD:-}" ]]; then
        if SIGNAL_FAILURE_MESSAGE="$message" bash -c "$SIGNAL_NOTIFY_CMD"; then
            return 0
        fi
        printf 'Signal notification delivery failed.\n' >&2
        delivery_status=1
    fi
    logger -t signal-collector -- "$message" 2>/dev/null || true
    printf '%s See %s\n' "$message" "$log" >&2
    return "$delivery_status"
}

run() {
    local flock_bin
    flock_bin="$(command -v flock)"
    mkdir -p "$(dirname -- "$LOCK")"
    exec 9>"$LOCK"
    "$flock_bin" -n 9 || exit 0

    if run_once; then
        return 0
    fi
    sleep "$RETRY_DELAY_SECONDS"
    if run_once; then
        return 0
    fi
    notify_failure || true
    return 1
}

check() {
    local report current previous=healthy pending='' exit_code=0 state_tmp
    mkdir -p "$(dirname -- "$LOCK")" "$(dirname -- "$HEALTH_LOG")" "$(dirname -- "$HEALTH_STATE")"
    exec 9>"$LOCK"
    flock -n 9 || return 0
    if report="$(timeout --kill-after=5 60 "$PYTHON" "$ROOT/signal/health.py" --db "$DB" --max-age-seconds "$STALE_SECONDS" \
        --dns-servers "${SIGNAL_DNS_SERVERS:-}" --gateway "${SIGNAL_GATEWAY:-}" \
        --external-ip "${SIGNAL_EXTERNAL_IP:-1.1.1.1}" 2>&1)"; then
        current=healthy
    else
        exit_code=$?
        current="${report%%$'\n'*}"
        if [[ "$exit_code" != 1 || ! "$current" =~ ^problem:[a-z_,]+$ ]]; then
            current=problem:health_check
        fi
    fi
    printf '%s %s\n%s\n' "$(date -Is)" "$current" "$report" >>"$HEALTH_LOG"
    if [[ -r "$HEALTH_STATE" ]]; then
        { IFS= read -r previous || true; IFS= read -r pending || true; } <"$HEALTH_STATE"
    fi
    if [[ "$current" != "$previous" ]]; then
        if [[ "$current" == healthy ]]; then
            pending="Signal health check recovered; previous problems: ${previous#problem:}."
        else
            pending="Signal health check detected ${current#problem:}. Check collector-run.sh status on the collection machine."
        fi
    fi
    if [[ -n "$pending" ]] && notify_failure "$pending" "$HEALTH_LOG" >>"$HEALTH_LOG" 2>&1; then
        pending=''
    fi
    state_tmp="$(mktemp "$HEALTH_STATE.XXXXXX")"
    printf '%s\n%s\n' "$current" "$pending" >"$state_tmp"
    mv -- "$state_tmp" "$HEALTH_STATE"
    printf '%s\n' "$report"
    return "$exit_code"
}

status() {
    local cron_entry last_check pending exit_code=0
    cron_entry="$(crontab -l 2>/dev/null | grep -F "$TAG" || true)"
    printf 'Cron: %s\n' "${cron_entry:-not installed}"
    [[ -n "$cron_entry" ]] || exit_code=1
    if ! printf '%s\n' "$cron_entry" | grep -Eq "$TAG$"; then
        printf 'Collector cron: not installed\n'
        exit_code=1
    fi
    if ! printf '%s\n' "$cron_entry" | grep -Fq "$TAG-health"; then
        printf 'Health cron: not installed (run install to add it)\n'
        exit_code=1
    fi
    if [[ -r "$HEALTH_STATE" ]]; then
        { IFS= read -r last_check || true; IFS= read -r pending || true; } <"$HEALTH_STATE"
        printf 'Last health check: %s\n' "$last_check"
        [[ "$last_check" == healthy ]] || exit_code=1
        if [[ -n "$pending" ]]; then
            printf 'Health notification: pending delivery\n'
            exit_code=1
        fi
    fi

    if [[ -n "${SIGNAL_HEALTHCHECK_URL:-}" ]]; then
        printf 'Healthchecks: configured (sent after recorded collection; see collector log for delivery)\n'
    else
        printf 'Healthchecks: not configured\n'
    fi

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
        printf 'Health: '
        "$PYTHON" "$ROOT/signal/health.py" --db "$DB" --max-age-seconds "$STALE_SECONDS" --status || exit_code=1
    else
        printf 'Latest attempt: no database at %s\n' "$DB"
        exit_code=1
    fi
    return "$exit_code"
}

install_cron() {
    local action cron_line health_line current speedtest
    speedtest="$(resolve_speedtest)" || return 1
    printf -v cron_line '*/15 * * * * SIGNAL_SPEEDTEST=%q %q run %s' "$speedtest" "$ROOT/signal/collector-run.sh" "$TAG"
    printf -v health_line '2-59/5 * * * * %q check >/dev/null 2>&1 %s-health' "$ROOT/signal/collector-run.sh" "$TAG"
    current="$(crontab -l 2>/dev/null || true)"
    if printf '%s\n' "$current" | grep -Fxq "$cron_line" && printf '%s\n' "$current" | grep -Fxq "$health_line"; then
        printf 'Signal cron jobs are already installed.\n'
        return 0
    fi
    action="Installed"
    if printf '%s\n' "$current" | grep -Fq "$TAG"; then
        action="Updated"
    fi
    {
        printf '%s\n' "$current" | awk -v tag="$TAG" 'index($0, tag) == 0'
        printf '%s\n' "$cron_line"
        printf '%s\n' "$health_line"
    } | crontab -
    printf '%s: %s\n%s\n' "$action" "$cron_line" "$health_line"
}

uninstall_cron() {
    local current
    current="$(crontab -l 2>/dev/null || true)"
    [[ -z "$current" ]] && return 0
    printf '%s\n' "$current" | awk -v tag="$TAG" 'index($0, tag) == 0' | crontab -
    printf 'Removed Signal cron jobs.\n'
}

case "${1:-run}" in
    run) run ;;
    check) check ;;
    status) status ;;
    install) install_cron ;;
    uninstall) uninstall_cron ;;
    *)
        printf 'Usage: %s [run|check|status|install|uninstall]\n' "$0" >&2
        exit 2
        ;;
esac

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
LOG="${SIGNAL_LOG:-$ROOT/signal/data/collector.log}"
LOCK="${SIGNAL_LOCK:-${XDG_RUNTIME_DIR:-/tmp}/signal-collector.lock}"
TIMEOUT_SECONDS="${SIGNAL_TIMEOUT_SECONDS:-300}"
RETRY_DELAY_SECONDS="${SIGNAL_RETRY_DELAY_SECONDS:-30}"
TAG="# signal-collector"
CRON_LINE="*/15 * * * * $ROOT/signal/collector-run.sh run $TAG"

run_once() {
    mkdir -p "$(dirname -- "$LOG")" "$(dirname -- "$LOCK")"
    "$(command -v timeout)" "$TIMEOUT_SECONDS" "$PYTHON" "$COLLECTOR" --once >>"$LOG" 2>&1
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

install_cron() {
    local current
    current="$(crontab -l 2>/dev/null || true)"
    if printf '%s\n' "$current" | grep -Fq "$TAG"; then
        printf 'Signal cron job is already installed.\n'
        return 0
    fi
    {
        printf '%s\n' "$current"
        printf '%s\n' "$CRON_LINE"
    } | crontab -
    printf 'Installed: %s\n' "$CRON_LINE"
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
    install) install_cron ;;
    uninstall) uninstall_cron ;;
    *)
        printf 'Usage: %s [run|install|uninstall]\n' "$0" >&2
        exit 2
        ;;
esac

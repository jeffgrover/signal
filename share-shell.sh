#!/usr/bin/env bash
set -e

session="${1:-signal-pi}"

if ! tmux has-session -t "$session" 2>/dev/null; then
    tmux new-session -d -s "$session" -c "$PWD"
fi
tmux set-window-option -t "$session" window-size largest
exec tmux attach-session -t "$session"

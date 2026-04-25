#!/usr/bin/env bash
# launchd target. Ensures orch tmux session exists, blocks while alive.
# Forwards exported env (BOT_USER, etc.) into the tmux session via setenv.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SOCKET_DIR=${TMPDIR:-/tmp}/claude-tmux-sockets
SOCKET="$SOCKET_DIR/deno-bot.sock"
mkdir -p "$SOCKET_DIR"

T() { tmux -S "$SOCKET" "$@"; }

if ! T has-session -t orch 2>/dev/null; then
  bash "$HERE/trust.sh" "$HERE" || true
  T start-server
  for v in BOT_USER BOT_EMAIL BOT_FORK UPSTREAM_REPO INTERVAL ROOT DENO WT_BASE PATH HOME; do
    val="${!v:-}"
    [ -n "$val" ] && T set-environment -g "$v" "$val"
  done
  # orch = bash shell. Claude runs inside via send-keys so its exit doesn't
  # kill the session (and respawn-storm launchd).
  T new-session -d -s orch -x 220 -y 60 -c "$HERE" bash -l
  sleep 1
  T send-keys -t orch:0.0 -- "bash $HERE/orchestrator.sh" Enter
fi

# Block until orch session goes away. launchd KeepAlive will relaunch.
while T has-session -t orch 2>/dev/null; do
  sleep 30
done

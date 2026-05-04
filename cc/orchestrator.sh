#!/usr/bin/env bash
# Orchestrator = a claude remote-control session, restarted on exit so a
# stale auth / network blip doesn't kill the orch session permanently.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

CLAUDE_BIN=${CLAUDE_BIN:-/Users/divy/.npm-packages/bin/claude}

while true; do
  echo "=== launching claude orchestrator $(date +%H:%M:%S) using $CLAUDE_BIN ==="
  "$CLAUDE_BIN" remote-control \
    --name "deno-bot:orchestrator" \
    --permission-mode bypassPermissions \
    --model sonnet
  rc=$?
  echo "claude exited rc=$rc — restart in 30s (Ctrl-C to stop)"
  sleep 30
done

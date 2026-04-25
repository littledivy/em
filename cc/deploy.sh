#!/usr/bin/env bash
# deploy cc/ to mac mini and install launchd job
set -euo pipefail

HOST="${1:-divys-mac-mini.local}"
DEST="${DEST:-/Users/divy/cc}"
INTERVAL="${INTERVAL:-900}"

HERE="$(cd "$(dirname "$0")" && pwd)"

echo "==> sync cc/ -> $HOST:$DEST"
ssh -o ConnectTimeout=15 "$HOST" "mkdir -p '$DEST'"
rsync -az --delete \
  --exclude '.git' \
  --exclude '*.log' \
  "$HERE/" "$HOST:$DEST/"

: "${BOT_USER:?set BOT_USER env (gh username for the bot account)}"
BOT_EMAIL="${BOT_EMAIL:-${BOT_USER}@users.noreply.github.com}"
BOT_FORK="${BOT_FORK:-${BOT_USER}/deno}"
UPSTREAM_REPO="${UPSTREAM_REPO:-denoland/deno}"

echo "==> install launchd on $HOST (BOT_USER=$BOT_USER fork=$BOT_FORK)"
ssh -o ConnectTimeout=15 "$HOST" "INTERVAL=$INTERVAL BOT_USER='$BOT_USER' BOT_EMAIL='$BOT_EMAIL' BOT_FORK='$BOT_FORK' UPSTREAM_REPO='$UPSTREAM_REPO' bash '$DEST/install.sh'"

echo "==> done."
echo "tail logs:    ssh $HOST 'tail -f ~/.deno-bot/logs/tick.{out,err}'"
echo "manual tick:  ssh $HOST 'python3 $DEST/tick.py'"
echo "halt:         ssh $HOST 'touch ~/.deno-bot/halt'"

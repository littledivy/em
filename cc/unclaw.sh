#!/usr/bin/env bash
# Manual unclaw worker spawner. No auto-pacing, no tick.py integration.
# Usage: bash /Users/divy/cc/unclaw.sh <slug> "<task description>"
set -euo pipefail

export PATH="/opt/homebrew/bin:$PATH"

SLUG="${1:?usage: unclaw.sh <slug> \"<task description>\"}"
DESC="${2:?usage: unclaw.sh <slug> \"<task description>\"}"

UPSTREAM=denoland/unclaw
USER_GH=littledivy
REPO_DIR="$HOME/src/unclaw"
WT_BASE="$HOME/src/unclaw-wt"
WT="$WT_BASE/$SLUG"
BR="claude/$SLUG"
SESSION="unc-$(printf '%s' "$SLUG" | tr -c 'A-Za-z0-9-' '-' | cut -c1-32)"
HERE="$(cd "$(dirname "$0")" && pwd)"

SOCKET_DIR=${TMPDIR:-/tmp}/claude-tmux-sockets
SOCKET="$SOCKET_DIR/deno-bot.sock"
mkdir -p "$SOCKET_DIR" "$WT_BASE"

T() { tmux -S "$SOCKET" "$@"; }
log() { echo "[$(date +%H:%M:%S)] $*"; }

# Auth: ensure littledivy token is available; don't change global active account.
GH_TOKEN=$(gh auth token --user "$USER_GH") || { echo "no gh auth for $USER_GH; run: gh auth login --user $USER_GH"; exit 1; }
export GH_TOKEN
export GIT_AUTHOR_NAME="$USER_GH" GIT_AUTHOR_EMAIL="${USER_GH}@users.noreply.github.com"
export GIT_COMMITTER_NAME="$USER_GH" GIT_COMMITTER_EMAIL="${USER_GH}@users.noreply.github.com"

# Clone if missing
if [ ! -d "$REPO_DIR/.git" ]; then
  log "cloning $UPSTREAM..."
  git clone "https://x-access-token:${GH_TOKEN}@github.com/${UPSTREAM}.git" "$REPO_DIR"
fi

git -C "$REPO_DIR" fetch origin main --quiet

# Add worktree
if [ -d "$WT" ]; then
  log "worktree $WT already exists; reusing"
else
  git -C "$REPO_DIR" worktree add -B "$BR" "$WT" origin/main
  git -C "$WT" config user.name "$USER_GH"
  git -C "$WT" config user.email "${USER_GH}@users.noreply.github.com"
fi

bash "$HERE/trust.sh" "$WT" || true

# Kill any prior session, spawn fresh
T kill-session -t "$SESSION" 2>/dev/null || true
T new-session -d -s "$SESSION" -x 200 -y 50 -c "$WT"
T send-keys -t "$SESSION":0.0 -- "/Users/divy/.npm-packages/bin/claude --permission-mode bypassPermissions --model sonnet -n 'unclaw:$SLUG'" Enter
sleep 8
T send-keys -t "$SESSION":0.0 -- "/remote-control" Enter
sleep 3

PROMPT=$(sed "s|<TASK>|$DESC|g" "$HERE/prompt-unclaw.md")
T set-buffer -b prompt -- "$PROMPT"
T paste-buffer -b prompt -t "$SESSION":0.0
sleep 1
T send-keys -t "$SESSION":0.0 Enter

log "spawned: $SESSION (worktree $WT, branch $BR)"
log "watch:  tmux -S $SOCKET attach -t $SESSION"
log "remote: visible at claude.ai/code as 'unclaw:$SLUG'"

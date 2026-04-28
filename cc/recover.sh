#!/usr/bin/env bash
# Recover the orchestrator claude session after a crash/reboot.
#
# Strategy: pick newest jsonl in ~/.claude/projects/-Users-divy-cc/
# (orchestrator cwd is /Users/divy/cc, so claude only lands sessions there).
# Stop the orchestrator.sh auto-respawn loop + any in-flight claude, then type
# `claude --resume <uuid>` into the orch tmux pane, dismiss the possible
# "resume from summary?" fork prompt with Enter, and finally `/remote-control`
# to expose the session on claude.ai/code.
#
# Usage:
#   bash cc/recover.sh                              # run locally on mini
#   HOST=divys-mac-mini.local bash cc/recover.sh    # run from dev mac

set -uo pipefail

HOST="${HOST:-}"   # empty = local
SOCK="${SOCK:-/var/folders/pz/g2pyqxtj0272rxj5tp96sdkc0000gn/T/claude-tmux-sockets/deno-bot.sock}"
PROJECT_DIR="${PROJECT_DIR:-/Users/divy/.claude/projects/-Users-divy-cc}"
CC_DIR="${CC_DIR:-/Users/divy/cc}"
CLAUDE_BIN="${CLAUDE_BIN:-/Users/divy/.npm-packages/bin/claude}"

SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"

# H: run a shell command on $HOST (or locally if HOST is empty).
# IdentitiesOnly + a single -i avoids "Too many authentication failures" when
# the local ssh-agent is loaded with many keys.
H() {
  if [ -n "$HOST" ]; then
    ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o ConnectTimeout=10 "$HOST" "$@"
  else
    bash -c "$*"
  fi
}

log() { echo "[recover] $*"; }

# 1. Find LARGEST jsonl (by bytes). The pre-crash orchestrator session is
# always far bigger than any fresh restart's jsonl, so picking by size beats
# picking by mtime — avoids the race where a fresh respawn touches a tiny
# jsonl right before we look.
log "looking for largest orchestrator session jsonl on ${HOST:-localhost}"
LATEST=$(H "ls -S '$PROJECT_DIR'/*.jsonl 2>/dev/null | head -1")
if [ -z "$LATEST" ]; then
  log "no jsonl in $PROJECT_DIR. diagnosing what claude has stored:"
  H "ls -1 ~/.claude/projects/ 2>/dev/null | head -20" || true
  log "refusing to start fresh — fix path / restore state and rerun"
  exit 1
fi
UUID=$(basename "$LATEST" .jsonl)
log "resuming uuid=$UUID (size $(H "ls -lh '$LATEST' 2>/dev/null | awk '{print \$5}'"))"

# 2. Stop legacy launchd job + any in-flight claude / orch loop. Idempotent.
log "stopping legacy launchd + orch loop + claude"
H "launchctl bootout gui/\$(id -u)/ai.deno-bot 2>/dev/null || true; pkill -f 'bash $CC_DIR/launcher.sh' 2>/dev/null || true; pkill -f 'bash $CC_DIR/orchestrator.sh' 2>/dev/null || true; pkill -f 'claude.*remote-control' 2>/dev/null || true; pkill -f 'claude --resume' 2>/dev/null || true"
sleep 2

# 3. Ensure orch tmux session exists. We create it inline (no launcher.sh /
# orchestrator.sh — those auto-loop a fresh claude which would race us).
TMUX_BIN="${TMUX_BIN:-/opt/homebrew/bin/tmux}"
if ! H "$TMUX_BIN -S '$SOCK' has-session -t orch 2>/dev/null"; then
  log "creating orch tmux session"
  H "mkdir -p \$(dirname '$SOCK'); $TMUX_BIN -S '$SOCK' new-session -d -s orch -x 220 -y 60 -c '$CC_DIR' bash -l"
  sleep 1
fi

# Interrupt whatever is currently in the orch pane (likely a dead claude).
H "$TMUX_BIN -S '$SOCK' send-keys -t orch:0.0 C-c 2>/dev/null || true"
sleep 1

# 4. Send the resume command
log "sending claude --resume into orch pane"
H "$TMUX_BIN -S '$SOCK' send-keys -t orch:0.0 -- \"cd $CC_DIR && $CLAUDE_BIN --resume $UUID --permission-mode bypassPermissions\" Enter"

# 5. Wait for claude TUI; dismiss possible fork-prompt with Enter
sleep 6
H "$TMUX_BIN -S '$SOCK' send-keys -t orch:0.0 Enter 2>/dev/null || true"
sleep 4

# 6. Enable remote-control for claude.ai/code (best-effort; newer claude may
# have removed/renamed this slash. Failure prints "Unknown command" in pane
# but is harmless — the local tmux session is fully usable either way.)
log "trying /remote-control (best-effort)"
H "$TMUX_BIN -S '$SOCK' send-keys -t orch:0.0 -- '/remote-control' Enter"
sleep 5

# 7. Show the result + URL
log "current orch pane:"
H "$TMUX_BIN -S '$SOCK' capture-pane -p -t orch:0.0 -S -60"

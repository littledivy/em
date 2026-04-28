#!/usr/bin/env bash
# Install the cc/ scripts. No launchd anymore — auto-respawn caused us to
# lose the prior orchestrator session every reboot (new claude session, new
# jsonl, prior conversation orphaned). Now you bring orch back manually with
# `bash cc/recover.sh` (resumes the previous session by uuid).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
LABEL="ai.deno-bot"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"

mkdir -p "$HOME/.deno-bot/logs"
chmod +x "$HERE/tick.py" "$HERE/recover.sh" \
         "$HERE/launcher.sh" "$HERE/orchestrator.sh" 2>/dev/null || true

# If a prior version installed launchd, tear it down. Idempotent.
if [ -f "$PLIST" ] || launchctl print "gui/$(id -u)/${LABEL}" >/dev/null 2>&1; then
  echo "removing legacy launchd job"
  launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
  rm -f "$PLIST"
fi

SOCK="${TMPDIR:-/tmp}/claude-tmux-sockets/deno-bot.sock"
echo "installed cc/ scripts (no launchd auto-respawn)"
echo "first start:  bash ${HERE}/recover.sh   # resumes prior orch session by uuid"
echo "watch orch:   tmux -S $SOCK attach -t orch"
echo "watch worker: tmux -S $SOCK attach -t dn-<task>     (or via claude.ai/code)"
echo "list:         tmux -S $SOCK ls"
echo "manual tick:  python3 ${HERE}/tick.py"
echo "halt:         touch ~/.deno-bot/halt"
echo "resume:       rm ~/.deno-bot/halt"

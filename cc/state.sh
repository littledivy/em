#!/usr/bin/env bash
# Emit current orchestrator state. Future-me runs this on every fresh
# session start to get caught up without replaying conversation history.
# Output is human-readable; pipe through `less` if long.
set -u

PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
DB=$HOME/.deno-bot/tasks.db
SOCK=$(ls /var/folders/pz/*/T/claude-tmux-sockets/deno-bot.sock 2>/dev/null | head -1)
EM=$HOME/gh/em

echo "# Orchestrator state — $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo

# Halt
if [ -f "$HOME/.deno-bot/halt" ]; then
  echo "## ⛔ HALT FLAG SET ($HOME/.deno-bot/halt)"
else
  echo "## ✅ Not halted"
fi
echo

# Active worker tmux sessions
echo "## Active tmux sessions"
if [ -n "$SOCK" ]; then
  tmux -S "$SOCK" ls 2>/dev/null | sed 's/^/- /'
else
  echo "  (no tmux socket found)"
fi
echo

# Tasks by status
echo "## Tasks by status (recent)"
sqlite3 -separator $'\t' "$DB" "
  SELECT status, COUNT(*) FROM tasks GROUP BY status ORDER BY COUNT(*) DESC
" | awk -F'\t' '{ printf "- %-10s %s\n", $1":", $2 }'
echo

# Open PRs in flight (review/running/monitoring)
echo "## Open-PR tasks (review/running/monitoring)"
sqlite3 -separator '|' "$DB" "
  SELECT id, status, attempts, COALESCE(host,'?'), COALESCE(cli,'?'), COALESCE(pr_url,'')
  FROM tasks
  WHERE status IN ('review','running','monitoring') AND pr_url IS NOT NULL AND pr_url != ''
  ORDER BY updated_at DESC
" | awk -F'|' '{ pr=$6; sub(".*pull/","#",pr); printf "- %-50s %-12s host=%-12s cli=%-6s att=%s %s\n", $1, $2, $4, $5, $3, pr }'
echo

# Tasks pending action (failed in last 24h with no PR — may need attention)
echo "## Failed/abandoned in last 24h (no PR)"
sqlite3 -separator '|' "$DB" "
  SELECT id, status, COALESCE(last_error,'')
  FROM tasks
  WHERE status IN ('failed','abandoned')
    AND (pr_url IS NULL OR pr_url = '')
    AND updated_at > strftime('%s','now')-86400
  ORDER BY updated_at DESC LIMIT 10
" | awk -F'|' '{ printf "- %-55s %-10s %s\n", $1, $2, substr($3,1,60) }'
echo

# Recent commits to em (orchestrator code history)
echo "## Recent commits to ~/gh/em (orchestrator code)"
git -C "$EM" log -10 --pretty=format:'- %h %s' 2>/dev/null
echo
echo

# Capacity check (run a no-op tick? no — just inspect HOSTS)
echo "## Capacity from vms.toml / py3.9 fallback"
python3 -c "
import sys
sys.path.insert(0, '/Users/divy/cc')
from tick import load_hosts
for h in load_hosts():
    print(f'- {h.name:<14} cap={h.capacity} clis={list(h.clis)} unclaw={h.unclaw_wrap}')
" 2>&1 | head -20
echo

# Quick sanity
echo "## How to drive (cheat sheet)"
cat <<'EOF'
- Run a tick:                python3 /Users/divy/cc/tick.py
- Peek a worker pane:        tmux -S $SOCK capture-pane -t dn-<task> -p
- Push a guidance msg:       echo "<msg>" > ~/.deno-bot/inbox/<task>.txt
- Halt new spawns:           touch ~/.deno-bot/halt
- Inspect a task:            sqlite3 ~/.deno-bot/tasks.db "SELECT * FROM tasks WHERE id='<task>';"
- Push code changes:         cp /Users/divy/cc/<file> ~/gh/em/cc/<file> && (cd ~/gh/em && git add cc/<file> && git commit -m 'cc: <msg>' && git push)
EOF

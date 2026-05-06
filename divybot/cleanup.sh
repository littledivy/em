#!/bin/bash
# cleanup.sh — sweep stale divybot worktrees and free Nix build artifacts
set -euo pipefail

DB="${DIVYBOT_ROOT:-$HOME/.divybot}/tasks.db"
WT_BASE="$HOME/src/deno-wt"
DENO_SRC="$HOME/src/deno"

if [ ! -f "$DB" ]; then
  echo "DB not found: $DB"
  exit 1
fi

RUNNING=$(sqlite3 "$DB" "SELECT id FROM tasks WHERE status='running'" | sed 's/nodecompat://')
REVIEW=$(sqlite3 "$DB" "SELECT id FROM tasks WHERE status='review'" | sed 's/nodecompat://')
KEEP="$RUNNING $REVIEW"

echo "=== Worktree sweep ==="
for dir in "$WT_BASE"/*/; do
  [ -d "$dir" ] || continue
  name=$(basename "$dir")
  if echo "$KEEP" | grep -qw "$name"; then
    if echo "$REVIEW" | grep -qw "$name"; then
      if [ -d "$dir/target" ]; then
        echo "  free target/: $name"
        rm -rf "$dir/target"
      fi
    else
      echo "  keep (running): $name"
    fi
  else
    echo "  delete: $name"
    rm -rf "$dir"
  fi
done

echo "=== Git worktree prune ==="
cd "$DENO_SRC" && git worktree prune

echo "=== Disk after ==="
df -h "$HOME"

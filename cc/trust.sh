#!/usr/bin/env bash
# Pre-mark a workspace as trusted in ~/.claude.json so claude skips dialog.
set -eu
DIR="${1:?usage: trust.sh <dir>}"
DIR="$(cd "$DIR" 2>/dev/null && pwd || echo "$DIR")"  # resolve to absolute
CLAUDE_JSON="${CLAUDE_JSON:-$HOME/.claude.json}"

[ -f "$CLAUDE_JSON" ] || echo '{}' > "$CLAUDE_JSON"

python3 - "$DIR" "$CLAUDE_JSON" <<'PY'
import json, sys
dir_path, conf = sys.argv[1], sys.argv[2]
with open(conf) as f: d = json.load(f)
d.setdefault('projects', {}).setdefault(dir_path, {})['hasTrustDialogAccepted'] = True
d['remoteDialogSeen'] = True
with open(conf, 'w') as f: json.dump(d, f, indent=2)
print(f"trusted: {dir_path}")
PY

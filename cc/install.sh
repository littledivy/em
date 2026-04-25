#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
LABEL="ai.deno-bot"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
INTERVAL="${INTERVAL:-900}"   # 15 min

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.deno-bot/logs"
chmod +x "$HERE/tick.py" "$HERE/launcher.sh" "$HERE/orchestrator.sh"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${HERE}/launcher.sh</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>${HOME}/.deno-bot/logs/launcher.out</string>
  <key>StandardErrorPath</key><string>${HOME}/.deno-bot/logs/launcher.err</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>HOME</key><string>${HOME}</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${HOME}/.npm-packages/bin:${HOME}/.cargo/bin</string>
    <key>BOT_USER</key><string>${BOT_USER:?set BOT_USER env when running install.sh}</string>
    <key>BOT_EMAIL</key><string>${BOT_EMAIL:-${BOT_USER}@users.noreply.github.com}</string>
    <key>BOT_FORK</key><string>${BOT_FORK:-${BOT_USER}/deno}</string>
    <key>UPSTREAM_REPO</key><string>${UPSTREAM_REPO:-denoland/deno}</string>
    <key>INTERVAL</key><string>${INTERVAL}</string>
  </dict>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

SOCK="${TMPDIR:-/tmp}/claude-tmux-sockets/deno-bot.sock"
echo "installed: ${LABEL} (orchestrator loops every ${INTERVAL}s)"
echo "watch orch:   tmux -S $SOCK attach -t orch"
echo "watch worker: tmux -S $SOCK attach -t dn-<task>     (or via claude.ai/code)"
echo "list:         tmux -S $SOCK ls"
echo "manual tick:  python3 ${HERE}/tick.py"
echo "halt:         touch ~/.deno-bot/halt"
echo "resume:       rm ~/.deno-bot/halt"
echo "logs:         tail -f ~/.deno-bot/logs/launcher.{out,err}"
echo "uninstall:    launchctl bootout gui/\$(id -u)/${LABEL} && rm '$PLIST'"

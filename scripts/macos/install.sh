#!/usr/bin/env bash
# Installiert den Harness als Hintergrunddienst (LaunchAgent) auf macOS.
# Startet automatisch beim Login und nach Abstürzen. Logs: ~/Library/Logs/SecondBrain/
set -euo pipefail

LABEL="com.secondbrain.harness"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/SecondBrain"
PORT="${PORT:-8100}"
HOST="${HOST:-127.0.0.1}"

UV="$(command -v uv || true)"
if [[ -z "$UV" ]]; then
  echo "❌ uv nicht gefunden. Installieren mit:  brew install uv" >&2
  exit 1
fi
command -v npx >/dev/null || echo "⚠️  npx nicht gefunden – für Node-basierte MCP-Server: brew install node"

cd "$REPO"
[[ -f .env ]] || { cp .env.example .env; echo "📝 .env angelegt – bitte LiteLLM-URL/Key etc. eintragen."; }
[[ -f config/mcp.json ]] || { echo '{ "mcpServers": {} }' > config/mcp.json; echo "📝 config/mcp.json angelegt (Beispiele: config/mcp.example.json)."; }
if [[ ! -f config/secrets.env ]]; then
  cp config/secrets.example.env config/secrets.env
  chmod 600 config/secrets.env
  echo "📝 config/secrets.env angelegt (nur für dich lesbar)."
fi

echo "📦 Abhängigkeiten installieren …"
"$UV" sync --quiet

mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"
# PATH der aktuellen Shell übernehmen, damit npx/uvx/node/git in MCP-Servern gefunden werden
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$UV</string><string>run</string><string>--no-sync</string>
    <string>uvicorn</string><string>app.main:app</string>
    <string>--host</string><string>$HOST</string><string>--port</string><string>$PORT</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$PATH</string>
    <key>HOME</key><string>$HOME</string>
    <key>LANG</key><string>${LANG:-de_DE.UTF-8}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOG_DIR/harness.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/harness.log</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "✅ Harness läuft auf http://$HOST:$PORT  (Admin/Doku: http://localhost:$PORT/docs)"
echo "   Logs:      tail -f $LOG_DIR/harness.log"
echo "   Neustart:  launchctl kickstart -k gui/$(id -u)/$LABEL"
echo "   Entfernen: scripts/macos/uninstall.sh"

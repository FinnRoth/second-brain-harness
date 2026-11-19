#!/usr/bin/env bash
# Installiert den Harness als Hintergrunddienst (LaunchAgent) auf macOS.
# Startet automatisch beim Login und nach Abstürzen. Logs: ~/Library/Logs/SecondBrain/
# Bitte OHNE sudo ausführen – der Dienst läuft als dein Benutzer.
set -euo pipefail

LABEL="com.secondbrain.harness"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/SecondBrain"
PORT="${PORT:-8100}"
HOST="${HOST:-127.0.0.1}"
DOMAIN="gui/$(id -u)"

if [[ "$(id -u)" -eq 0 ]]; then
  echo "❌ Bitte nicht mit sudo ausführen – der Dienst muss als dein Benutzer laufen:" >&2
  echo "   scripts/macos/install.sh" >&2
  echo "   Falls du es schon mit sudo versucht hast, vorher einmal die Rechte zurücksetzen:" >&2
  echo "   sudo chown -R \"\$(id -un)\" \"$REPO\" ~/Library/LaunchAgents ~/Library/Logs/SecondBrain 2>/dev/null" >&2
  exit 1
fi

# Dateien, die einem anderen Benutzer gehören (z. B. von einem sudo-Lauf), blockieren alles Weitere
for p in "$REPO/.venv" "$REPO/web/node_modules" "$REPO/web/dist" "$REPO/data" "$PLIST" "$LOG_DIR"; do
  if [[ -e "$p" ]] && [[ -n "$(find "$p" -maxdepth 2 ! -user "$(id -u)" -print -quit 2>/dev/null)" ]]; then
    echo "❌ $p gehört (teilweise) einem anderen Benutzer – vermutlich von einem Lauf mit sudo." >&2
    echo "   Einmal reparieren mit:" >&2
    echo "   sudo chown -R \"$(id -un)\" \"$REPO\" ~/Library/LaunchAgents ~/Library/Logs/SecondBrain 2>/dev/null; scripts/macos/install.sh" >&2
    exit 1
  fi
done

UV="$(command -v uv || true)"
if [[ -z "$UV" ]]; then
  echo "❌ uv nicht gefunden. Installieren mit:  brew install uv" >&2
  exit 1
fi
if ! command -v npm >/dev/null; then
  echo "❌ node/npm nicht gefunden (wird für die App und Node-basierte MCP-Server gebraucht):  brew install node" >&2
  exit 1
fi

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

echo "🎨 App bauen …"
(cd web && npm ci --no-audit --no-fund --silent && npm run build --silent)

# ------------------------------------------------------------------ LaunchAgent
xml() { # Werte für die Plist escapen (PATH kann z. B. & enthalten)
  printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/"/\&quot;/g'
}

mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"
TMP_PLIST="$(mktemp -t sbharness)"
# PATH der aktuellen Shell übernehmen, damit npx/uvx/node/git in MCP-Servern gefunden werden
cat > "$TMP_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml "$UV")</string><string>run</string><string>--no-sync</string>
    <string>uvicorn</string><string>app.main:app</string>
    <string>--host</string><string>$(xml "$HOST")</string><string>--port</string><string>$(xml "$PORT")</string>
  </array>
  <key>WorkingDirectory</key><string>$(xml "$REPO")</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$(xml "$PATH")</string>
    <key>HOME</key><string>$(xml "$HOME")</string>
    <key>LANG</key><string>$(xml "${LANG:-de_DE.UTF-8}")</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Interactive</string>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$(xml "$LOG_DIR/harness.log")</string>
  <key>StandardErrorPath</key><string>$(xml "$LOG_DIR/harness.log")</string>
</dict>
</plist>
PLIST
if ! plutil -lint "$TMP_PLIST" >/dev/null; then
  echo "❌ Die erzeugte LaunchAgent-Datei ist ungültig:" >&2
  plutil -lint "$TMP_PLIST" >&2 || true
  exit 1
fi

loaded() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }

echo "🚀 Hintergrunddienst einrichten …"
# Alte Version entladen und warten, bis launchd sie wirklich losgelassen hat (bootout ist asynchron)
if loaded; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  for _ in $(seq 1 20); do loaded || break; sleep 0.5; done
fi
mv -f "$TMP_PLIST" "$PLIST"
chmod 644 "$PLIST"
launchctl enable "$DOMAIN/$LABEL" 2>/dev/null || true

ok=0
for attempt in 1 2 3; do
  if out="$(launchctl bootstrap "$DOMAIN" "$PLIST" 2>&1)"; then ok=1; break; fi
  if loaded; then ok=1; break; fi   # „schon geladen“ ist auch gut
  sleep 2
done
if [[ $ok -eq 0 ]]; then
  # Fallback auf die ältere Schnittstelle
  launchctl load -w "$PLIST" 2>/dev/null && loaded && ok=1
fi
if [[ $ok -eq 0 ]]; then
  echo "❌ launchd hat den Dienst nicht übernommen: $out" >&2
  echo "" >&2
  echo "   Mögliche Ursachen:" >&2
  echo "   • Firmen-Mac mit Verwaltung (MDM), die eigene Hintergrunddienste blockiert" >&2
  echo "     → Systemeinstellungen › Allgemein › Anmeldeobjekte: „uv“/„Second Brain“ erlauben" >&2
  echo "   • Terminal läuft nicht in deiner normalen Anmeldesitzung (z. B. per SSH/tmux)" >&2
  echo "" >&2
  echo "   Diagnose:  launchctl print $DOMAIN/$LABEL ; log show --last 5m --predicate 'process == \"launchd\"' | grep -i secondbrain" >&2
  echo "   Ohne Hintergrunddienst starten:  scripts/macos/run.sh" >&2
  exit 1
fi

printf "⏳ Warte auf den Start "
for _ in $(seq 1 40); do
  curl -fs "http://localhost:$PORT/health" >/dev/null 2>&1 && break
  printf "."
  sleep 1
done
echo
if ! curl -fs "http://localhost:$PORT/health" >/dev/null 2>&1; then
  echo "⚠️  Der Dienst ist eingerichtet, antwortet aber noch nicht. Letzte Log-Zeilen:"
  tail -n 25 "$LOG_DIR/harness.log" 2>/dev/null || true
  exit 1
fi

echo "✅ Second Brain läuft:  http://localhost:$PORT"
echo "   Als App installieren: in Safari „Ablage → Zum Dock hinzufügen“ (oder in Chrome: Installieren-Symbol in der Adresszeile)"
echo "   API-Doku:  http://localhost:$PORT/docs"
echo "   Logs:      tail -f $LOG_DIR/harness.log"
echo "   Neustart:  launchctl kickstart -k $DOMAIN/$LABEL"
echo "   Update:    git pull && scripts/macos/install.sh"
echo "   Entfernen: scripts/macos/uninstall.sh"
open "http://localhost:$PORT" 2>/dev/null || true

#!/usr/bin/env bash
# Entfernt den LaunchAgent. Daten (data/, config/) bleiben erhalten.
set -euo pipefail
LABEL="com.secondbrain.harness"
if [[ "$(id -u)" -eq 0 ]]; then echo "❌ Bitte ohne sudo ausführen." >&2; exit 1; fi
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload "$HOME/Library/LaunchAgents/$LABEL.plist" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "✅ LaunchAgent entfernt. Daten in data/ und config/ sind unverändert."

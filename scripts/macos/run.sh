#!/usr/bin/env bash
# Startet den Harness im Vordergrund (ohne Hintergrunddienst) – z. B. wenn launchd
# auf einem verwalteten Mac blockiert ist. Beenden mit Ctrl+C.
set -euo pipefail
cd "$(dirname "$0")/../.."
if [[ "$(id -u)" -eq 0 ]]; then echo "❌ Bitte ohne sudo ausführen." >&2; exit 1; fi
[[ -d web/dist ]] || (cd web && npm ci --no-audit --no-fund --silent && npm run build --silent)
PORT="${PORT:-8100}"
( sleep 3; open "http://localhost:$PORT" 2>/dev/null || true ) &
exec uv run uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "$PORT"

"""Kleiner MCP-Testserver (stdio): Notizen in einer Datei."""

import json
import os
import sys
from pathlib import Path

from mcp.server import MCPServer
from mcp_types import ToolAnnotations

NOTES = Path(os.environ["NOTES_FILE"])
server = MCPServer("notes", instructions="Verwaltet Notizen des Nutzers.")


def _load() -> list[str]:
    return json.loads(NOTES.read_text()) if NOTES.exists() else []


@server.tool(annotations=ToolAnnotations(read_only_hint=True))
def list_notes() -> str:
    """Listet alle Notizen."""
    notes = _load()
    return "\n".join(notes) if notes else "(keine Notizen)"


@server.tool()
def add_note(text: str) -> str:
    """Legt eine neue Notiz an."""
    notes = _load()
    notes.append(text)
    NOTES.write_text(json.dumps(notes))
    return f"Notiz gespeichert ({len(notes)} insgesamt)"


@server.tool()
def secret_token() -> str:
    """Gibt den konfigurierten Token zurück (Test für ${VAR}-Ersetzung)."""
    return os.environ.get("NOTES_TOKEN", "")


@server.tool()
def crash() -> str:
    """Beendet den Server (Test für automatischen Neustart)."""
    print("crash requested", file=sys.stderr, flush=True)
    os._exit(1)


if __name__ == "__main__":
    print("notes server starting", file=sys.stderr, flush=True)
    if len(sys.argv) > 2 and sys.argv[1] == "--http":
        server.run("streamable-http", host="127.0.0.1", port=int(sys.argv[2]))
    else:
        server.run("stdio")

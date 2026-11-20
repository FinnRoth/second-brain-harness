import json
import sys
from pathlib import Path

import pytest

from app.mcp import editor
from app.mcp.config import ConfigError

SERVER = str(Path(__file__).parent / "mcp_fixtures" / "notes_server.py")


def test_editor_roundtrip(tmp_path):
    cfg = tmp_path / "mcp.json"
    editor.set_server(cfg, "a", {"command": "npx", "args": ["-y", "x"], "env": {}, "tools": {"deny": []}})
    assert json.loads(cfg.read_text()) == {"mcpServers": {"a": {"command": "npx", "args": ["-y", "x"]}}}
    editor.set_server(cfg, "b", {"type": "http", "url": "https://x/mcp", "headers": {"Authorization": "Bearer ${TOK}"}})
    editor.set_server(cfg, "a2", {"command": "uvx", "disabled": True}, previous_name="a")
    assert list(json.loads(cfg.read_text())["mcpServers"]) == ["a2", "b"]
    with pytest.raises(ConfigError):
        editor.set_server(cfg, "kaputt", {"type": "http"})  # url fehlt
    with pytest.raises(ConfigError):
        editor.set_server(cfg, "a b", {"command": "x"})
    assert editor.import_servers(cfg, {"mcpServers": {"c": {"command": "node", "args": ["s.js"]}}}) == ["c"]
    with pytest.raises(ConfigError):
        editor.import_servers(cfg, {"command": "x"})
    assert editor.delete_server(cfg, "b") and not editor.delete_server(cfg, "b")
    assert editor.referenced_vars(cfg) == set()


def test_secrets_file(tmp_path):
    sec = tmp_path / "secrets.env"
    sec.write_text("# Kommentar\nOTHER=1\n")
    editor.set_secret(sec, "TOKEN", "a b'c")
    editor.set_secret(sec, "PLAIN", "abc123")
    assert sec.read_text() == "# Kommentar\nOTHER=1\nTOKEN=\"a b'c\"\nPLAIN=abc123\n"
    assert oct(sec.stat().st_mode & 0o777) == "0o600"
    editor.set_secret(sec, "TOKEN", None)
    assert "TOKEN" not in sec.read_text()
    with pytest.raises(ConfigError):
        editor.set_secret(sec, "1BAD", "x")
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"x": {"command": "y", "env": {"K": "${NEEDED}"}}}}))
    names = {s["name"]: s for s in editor.list_secrets(sec, cfg)}
    assert names["NEEDED"] == {"name": "NEEDED", "set": False, "source": None, "used": True}
    assert names["PLAIN"]["set"] is True and "value" not in names["PLAIN"]


async def test_configure_server_via_api(services, client, settings, tmp_path):
    notes = tmp_path / "n.json"
    body = {"name": "notes", "config": {"command": sys.executable, "args": [SERVER],
                                         "env": {"NOTES_FILE": str(notes), "NOTES_TOKEN": "${NOTES_TOKEN}"}}}
    r = await client.put("/mcp/config/servers", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["servers"][0]["status"] == "ready"
    cfg = (await client.get("/mcp/config")).json()
    assert cfg["servers"]["notes"]["args"] == [SERVER]
    assert cfg["secrets"] == [{"name": "NOTES_TOKEN", "set": False, "source": None, "used": True}]

    r = await client.put("/mcp/secrets/NOTES_TOKEN", json={"value": "geheim"})
    assert r.json() == {"name": "NOTES_TOKEN", "set": True}
    assert "geheim" not in json.dumps((await client.get("/mcp/config")).json())
    conn = services.mcp.connections["notes"]
    assert (await conn.call_tool("secret_token", {})).content[0].text == "geheim"  # neu gestartet mit Secret

    body["config"]["disabled"] = True
    r = await client.put("/mcp/config/servers", json=body)
    assert r.json()["servers"][0]["status"] == "disabled"
    bad = await client.put("/mcp/config/servers", json={"name": "x", "config": {"type": "http"}})
    assert bad.status_code == 400 and "url" in bad.json()["detail"]
    assert (await client.delete("/mcp/config/servers/notes")).json()["servers"] == []
    assert (await client.delete("/mcp/config/servers/notes")).status_code == 404

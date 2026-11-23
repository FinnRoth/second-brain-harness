import json
import sys
from pathlib import Path

from app import bundles

SERVER = str(Path(__file__).parent / "mcp_fixtures" / "notes_server.py")


def test_path_warnings():
    assert bundles.path_warnings({"command": "npx", "args": ["-y", "pkg"]}) == []
    w = bundles.path_warnings({"command": "node", "args": ["/Users/finn/mcp/server.js"], "env": {"X": "~/data"}})
    assert len(w) == 2


async def test_export_then_import_elsewhere(services, client, settings, tmp_path):
    notes = tmp_path / "notes.json"
    await client.put("/mcp/config/servers", json={"name": "notes", "config": {
        "command": sys.executable, "args": [SERVER], "env": {"NOTES_FILE": str(notes), "NOTES_TOKEN": "${NOTES_TOKEN}"}}})
    await client.put("/mcp/secrets/NOTES_TOKEN", json={"value": "streng-geheim"})
    await client.put("/api/tools/mcp__notes__add_note/policy", json={"policy": "allow"})
    ref = (await client.post("/api/references", json={"title": "Interne Links", "description": "Links", "content": "- Jira: https://jira"})).json()
    await client.put("/api/settings", json={"custom_instructions": "Sei knapp."})

    ex = (await client.get("/api/bundles/exportable")).json()
    assert ex["servers"][0]["name"] == "notes" and ex["servers"][0]["secrets"] == ["NOTES_TOKEN"]
    assert ex["servers"][0]["policies"] == 1 and ex["servers"][0]["path_warnings"]

    r = await client.post("/api/bundles/export", json={"name": "SAP-Links", "servers": ["notes"], "references": [ref["id"]]})
    bundle = r.json()
    text = json.dumps(bundle)
    assert "streng-geheim" not in text and "${NOTES_TOKEN}" in text
    assert bundle["format"] == "second-brain-bundle" and bundle["toolPolicies"] == {"notes": {"add_note": "allow"}}
    assert bundle["references"][0]["content"] == "- Jira: https://jira" and "customInstructions" not in bundle

    # „Anderer Rechner“: alles lokal entfernen
    await client.delete("/mcp/config/servers/notes")
    await client.delete("/mcp/secrets/NOTES_TOKEN")
    await client.delete(f"/api/references/{ref['id']}")
    await client.put("/api/tools/mcp__notes__add_note/policy", json={"policy": None})

    pv = (await client.post("/api/bundles/preview", json=bundle)).json()
    assert pv["name"] == "SAP-Links" and pv["missing_secrets"] == ["NOTES_TOKEN"]
    assert pv["servers"][0]["exists"] is False and pv["references"][0]["exists"] is False

    r = await client.post("/api/bundles/import", json={"bundle": bundle, "decisions": {"secrets": {"NOTES_TOKEN": "neu"}}})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["result"]["servers"] == ["notes"] and res["result"]["references"] == ["Interne Links"]
    assert res["mcp"]["servers"][0]["status"] == "ready"
    conn = services.mcp.connections["notes"]
    assert (await conn.call_tool("secret_token", {})).content[0].text == "neu"
    assert services.store.tool_policies() == {"mcp__notes__add_note": "allow"}

    # Zweiter Import: Konflikte → Standard umbenennen, explizit überspringen/überschreiben
    pv = (await client.post("/api/bundles/preview", json=bundle)).json()
    assert pv["servers"][0]["exists"] and pv["servers"][0]["suggested_name"] == "notes-2"
    assert pv["references"][0]["exists"] and pv["missing_secrets"] == []
    r = await client.post("/api/bundles/import", json={"bundle": bundle, "decisions": {
        "servers": {"notes": {"action": "rename", "new_name": "notes-b"}},
        "references": {"0": {"action": "overwrite"}}}})
    res = r.json()["result"]
    assert res["servers"] == ["notes-b"] and res["references"] == ["Interne Links"]
    assert len((await client.get("/api/references")).json()) == 1
    assert services.store.tool_policies()["mcp__notes-b__add_note"] == "allow"


async def test_import_plain_claude_config_and_errors(services, client):
    plain = {"mcpServers": {"x": {"command": "gibt-es-nicht-xyz"}}}
    pv = (await client.post("/api/bundles/preview", json=plain)).json()
    assert pv["servers"][0]["name"] == "x"
    assert (await client.post("/api/bundles/preview", json={"format": "anders"})).status_code == 400
    assert (await client.post("/api/bundles/preview", json={"mcpServers": {"y": {"type": "http"}}})).status_code == 400
    r = await client.post("/api/bundles/import", json={"bundle": {"customInstructions": "Neu"}, "decisions": {"custom_instructions": "append"}})
    assert r.status_code == 200 and services.store.get_custom_instructions() == "Neu"

from conftest import FakeLLM, tool_call

from app.tools.base import ToolContext
from app.tools.references import search_lines

DIRECTORY = """# Interne Links

## Projektmanagement
- Jira Board Team Alpha: https://jira.intern.example/alpha – Tickets und Sprints
- Confluence Space Alpha: https://wiki.intern.example/alpha – Doku

## HR
- Urlaubsantrag: https://hr.intern.example/urlaub
- Reisekosten: https://hr.intern.example/reise – Abrechnung von Dienstreisen
"""


def test_search_lines_returns_matches_with_context():
    out = search_lines(DIRECTORY, "Reisekosten abrechnen")
    assert "Reisekosten: https://hr.intern.example/reise" in out
    assert "Jira" not in out
    assert out.splitlines()[0].split(":")[0].isdigit()
    assert search_lines(DIRECTORY, "xyz") is None


async def test_settings_and_references_api(services, client):
    r = await client.put("/api/settings", json={"custom_instructions": "Antworte knapp. Nutze SAP-Begriffe."})
    assert r.json()["custom_instructions"].startswith("Antworte knapp")
    assert (await client.get("/api/settings")).json()["custom_instructions"].endswith("SAP-Begriffe.")

    r = await client.post("/api/references", json={
        "title": "Interne Links", "description": "Verzeichnis interner URLs", "content": DIRECTORY})
    assert r.status_code == 201
    doc = r.json()
    assert doc["mode"] == "on_demand" and doc["chars"] == len(DIRECTORY)
    listing = (await client.get("/api/references")).json()
    assert "content" not in listing[0]
    r = await client.patch(f"/api/references/{doc['id']}", json={"enabled": False})
    assert r.json()["enabled"] is False
    assert (await client.delete(f"/api/references/{doc['id']}")).status_code == 200
    assert (await client.get("/api/references")).json() == []


async def test_system_prompt_includes_instructions_and_references(services):
    store = services.store
    store.set_custom_instructions("Sprich mich mit Du an.")
    store.create_reference_doc("Kürzel", "Firmenabkürzungen", "BTP = Business Technology Platform", "always")
    store.create_reference_doc("Interne Links", "Verzeichnis interner URLs", DIRECTORY, "on_demand")
    store.create_reference_doc("Alt", "deaktiviert", "geheim", "always", enabled=False)
    system, _ = services.prompt_builder.system_prompt(None)
    assert "Persönliche Anweisungen" in system and "Sprich mich mit Du an." in system
    assert "BTP = Business Technology Platform" in system  # immer im Kontext
    assert "**Interne Links**" in system and "Verzeichnis interner URLs" in system
    assert "jira.intern.example" not in system  # bei Bedarf → nicht im Prompt
    assert "geheim" not in system


async def test_read_reference_tool_in_chat(services, client, fake_llm: FakeLLM):
    store = services.store
    store.create_reference_doc("Interne Links", "Verzeichnis interner URLs", DIRECTORY, "on_demand")
    tools = services.tools.resolve(ToolContext("c", permission_mode="plan")).tools
    assert tools["read_reference"].decision == "allow"

    calls = {"n": 0}

    def handler(payload):
        calls["n"] += 1
        if calls["n"] == 1:
            assert "read_reference" in {t["function"]["name"] for t in payload["tools"]}
            return {"content": "", "tool_calls": [tool_call("read_reference", {"name": "interne links", "query": "jira"})]}
        result = payload["messages"][-1]["content"]
        assert "https://jira.intern.example/alpha" in result and "Urlaubsantrag" not in result
        return {"content": "Hier: https://jira.intern.example/alpha"}

    fake_llm.handler = handler
    r = await client.post("/api/chats", json={})
    cid = r.json()["id"]
    r = await client.post(f"/api/chats/{cid}/messages", json={"content": "Wo ist unser Jira-Board?"})
    import asyncio

    await asyncio.wait_for(services.runs.runs[r.json()["run_id"]].task, 10)
    msg = (await client.get(f"/api/chats/{cid}")).json()["messages"][1]
    tool = next(p for p in msg["parts"] if p["type"] == "tool")
    assert tool["name"] == "read_reference" and tool["status"] == "done"
    assert msg["content"].startswith("Hier:")


async def test_unknown_reference_lists_available(services):
    from app.tools.references import ReferenceToolProvider

    services.store.create_reference_doc("Interne Links", "", DIRECTORY, "on_demand")
    res = await ReferenceToolProvider(services.store).call("read_reference", {"name": "Gibts nicht"}, ToolContext("c"))
    assert res.is_error and "Interne Links (R1)" in res.text

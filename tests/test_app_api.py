import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest
from conftest import FakeLLM, tool_call

from app.main import create_app
from app.services import Services

SERVER = str(Path(__file__).parent / "mcp_fixtures" / "notes_server.py")


async def wait_run(services, run_id, timeout=10):
    run = services.runs.runs[run_id]
    await asyncio.wait_for(asyncio.shield(run.task), timeout)
    return run


async def wait_event(services, run_id, type_, timeout=10):
    run = services.runs.runs[run_id]
    for _ in range(int(timeout * 50)):
        for ev in run.events:
            if ev["type"] == type_:
                return ev
        await asyncio.sleep(0.02)
    raise AssertionError(f"Event {type_} kam nicht: {[e['type'] for e in run.events]}")


def chat_calls(fake_llm):
    return [c for c in fake_llm.calls if "Chat-Titel" not in c["messages"][0]["content"]]


def types(run):
    return [e["type"] for e in run.events]


async def new_chat(client, **kw):
    r = await client.post("/api/chats", json=kw)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_basic_chat_flow(services, client, fake_llm: FakeLLM):
    fake_llm.handler = lambda p: {"content": "Hallo Alex, schön dich zu sehen!"}
    cid = await new_chat(client)
    r = await client.post(f"/api/chats/{cid}/messages", json={"content": "Hi!", "reasoning_effort": "low"})
    assert r.status_code == 202
    run = await wait_run(services, r.json()["run_id"])
    assert types(run)[0] == "start" and types(run)[-1] == "end"
    assert {"text", "done", "saved", "title"} <= set(types(run))
    assert fake_llm.calls[0]["reasoning_effort"] == "low"

    # Event-Stream lässt sich (auch nachträglich) komplett abspielen
    sse = await client.get(f"/api/runs/{run.id}/events")
    seqs = [json.loads(line[6:])["seq"] for line in sse.text.splitlines() if line.startswith("data: ")]
    assert seqs == list(range(len(run.events)))

    chat = (await client.get(f"/api/chats/{cid}")).json()
    assert chat["title"] == "Testtitel" and chat["active_run"] is None
    assert [m["role"] for m in chat["messages"]] == ["user", "assistant"]
    assert chat["messages"][1]["parts"] == [{"type": "text", "text": "Hallo Alex, schön dich zu sehen!"}]
    # Fürs Gedächtnis wurde ein Turn gespeichert + Dreaming geplant
    assert services.store.turns_for_conversation(cid)[0]["user_message"] == "Hi!"
    assert any(j["type"] == "dream_conversation" for j in services.store.list_jobs(status="pending"))
    listing = (await client.get("/api/chats")).json()
    assert listing[0]["id"] == cid and listing[0]["title"] == "Testtitel"


async def test_parts_include_reasoning_and_tools(services, client, fake_llm: FakeLLM):
    services.settings.upstream_api = "responses"
    services.store.add_fact("Alex segelt gern", [services.store.self_id()])

    def handler(payload):
        if not any(i.get("type") == "function_call_output" for i in payload["input"]):
            return {"reasoning": "Ich schaue nach.", "tool_calls": [tool_call("search_memory", {"query": "Alex segelt"})]}
        return {"reasoning": "Gefunden.", "content": "Du segelst gern."}

    fake_llm.responses_handler = handler
    cid = await new_chat(client)
    r = await client.post(f"/api/chats/{cid}/messages", json={"content": "Was sind meine Hobbys?", "reasoning_effort": "high"})
    await wait_run(services, r.json()["run_id"])
    parts = (await client.get(f"/api/chats/{cid}")).json()["messages"][1]["parts"]
    assert [p["type"] for p in parts] == ["reasoning", "tool", "reasoning", "text"]
    assert parts[1]["name"] == "search_memory" and parts[1]["status"] == "done" and "segelt" in parts[1]["result"]
    assert parts[3]["text"] == "Du segelst gern."


@pytest.fixture
async def mcp_app(settings, fake_llm, tmp_path):
    notes = tmp_path / "notes.json"
    Path(settings.mcp_config).write_text(json.dumps({"mcpServers": {"notes": {
        "command": sys.executable, "args": [SERVER], "env": {"NOTES_FILE": str(notes)}}}}))
    svc = Services(settings, llm=fake_llm)
    await svc.startup()
    svc.notes_file = notes
    app = create_app(services=svc)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield svc, c
    await svc.shutdown()


def _notes_handler():
    def handler(payload):
        if payload["messages"][-1]["role"] == "user":
            return {"content": "", "tool_calls": [tool_call("mcp__notes__add_note", {"text": "Brot"})]}
        return {"content": "Erledigt." if "gespeichert" in payload["messages"][-1]["content"] else "Abgebrochen."}
    return handler


async def test_approval_buttons(mcp_app, fake_llm: FakeLLM):
    services, client = mcp_app
    fake_llm.handler = _notes_handler()
    cid = await new_chat(client)

    # 1) einmal erlauben
    r = await client.post(f"/api/chats/{cid}/messages", json={"content": "Notiere Brot"})
    rid = r.json()["run_id"]
    ev = await wait_event(services, rid, "approval_required")
    assert ev["name"] == "mcp__notes__add_note" and ev["args"] == {"text": "Brot"}
    assert not services.notes_file.exists()
    chat = (await client.get(f"/api/chats/{cid}")).json()
    assert chat["active_run"]["id"] == rid
    r = await client.post(f"/api/approvals/{ev['approval_id']}", json={"decision": "allow", "scope": "chat"})
    assert r.status_code == 200
    run = await wait_run(services, rid)
    assert json.loads(services.notes_file.read_text()) == ["Brot"]
    tool = next(p for p in (await client.get(f"/api/chats/{cid}")).json()["messages"][1]["parts"] if p["type"] == "tool")
    assert tool["status"] == "done" and tool["approved"] is True
    assert "approval_resolved" in types(run)

    # 2) „für diesen Chat erlaubt“ → kein erneutes Nachfragen
    r = await client.post(f"/api/chats/{cid}/messages", json={"content": "Notiere nochmal Brot"})
    run = await wait_run(services, r.json()["run_id"])
    assert "approval_required" not in types(run)
    assert json.loads(services.notes_file.read_text()) == ["Brot", "Brot"]

    # 3) anderer Chat fragt wieder – ablehnen
    cid2 = await new_chat(client)
    r = await client.post(f"/api/chats/{cid2}/messages", json={"content": "Notiere Brot"})
    rid = r.json()["run_id"]
    ev = await wait_event(services, rid, "approval_required")
    await client.post(f"/api/approvals/{ev['approval_id']}", json={"decision": "deny"})
    await wait_run(services, rid)
    msg = (await client.get(f"/api/chats/{cid2}")).json()["messages"][1]
    assert next(p for p in msg["parts"] if p["type"] == "tool")["status"] == "denied"
    assert msg["content"] == "Abgebrochen."
    assert len(json.loads(services.notes_file.read_text())) == 2

    # 4) „immer erlauben“ gilt global
    cid3 = await new_chat(client)
    r = await client.post(f"/api/chats/{cid3}/messages", json={"content": "Notiere Brot"})
    ev = await wait_event(services, r.json()["run_id"], "approval_required")
    await client.post(f"/api/approvals/{ev['approval_id']}", json={"decision": "allow", "scope": "always"})
    await wait_run(services, r.json()["run_id"])
    tools = {t["name"]: t for t in (await client.get("/api/tools")).json()}
    assert tools["mcp__notes__add_note"]["override"] == "allow"
    cid4 = await new_chat(client)
    r = await client.post(f"/api/chats/{cid4}/messages", json={"content": "Notiere Brot"})
    run = await wait_run(services, r.json()["run_id"])
    assert "approval_required" not in types(run)


async def test_plan_mode_per_chat(mcp_app, fake_llm: FakeLLM):
    services, client = mcp_app
    seen = {}

    def handler(p):
        seen["tools"] = {t["function"]["name"] for t in p.get("tools", [])}
        return {"content": "Plan."}

    fake_llm.handler = handler
    cid = await new_chat(client, permission_mode="plan")
    r = await client.post(f"/api/chats/{cid}/messages", json={"content": "Plane"})
    await wait_run(services, r.json()["run_id"])
    assert "mcp__notes__add_note" not in seen["tools"] and "mcp__notes__list_notes" in seen["tools"]
    r = await client.post(f"/api/chats/{cid}/messages", json={"content": "Jetzt los", "permission_mode": "auto"})
    await wait_run(services, r.json()["run_id"])
    assert "mcp__notes__add_note" in seen["tools"]
    assert (await client.get(f"/api/chats/{cid}")).json()["permission_mode"] == "auto"


async def test_cancel_saves_partial_answer(services, client, fake_llm: FakeLLM):
    async def slow(payload):
        yield {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "Teil 1 "}}]}
        await asyncio.sleep(30)
        yield {"choices": [{"index": 0, "delta": {"content": "nie"}}]}

    fake_llm.chat_stream = slow
    cid = await new_chat(client)
    r = await client.post(f"/api/chats/{cid}/messages", json={"content": "Erzähl was Langes"})
    rid = r.json()["run_id"]
    await wait_event(services, rid, "text")
    assert (await client.post(f"/api/chats/{cid}/messages", json={"content": "x"})).status_code == 409
    assert (await client.post(f"/api/runs/{rid}/cancel")).status_code == 200
    run = await wait_run(services, rid)
    assert run.status == "cancelled"
    msg = (await client.get(f"/api/chats/{cid}")).json()["messages"][1]
    assert msg["status"] == "cancelled" and msg["content"] == "Teil 1 "


async def test_edit_and_regenerate(services, client, fake_llm: FakeLLM):
    answers = iter(["A1", "A2", "A3", "A4"])
    fake_llm.handler = lambda p: {"content": next(answers)}
    cid = await new_chat(client)
    for text in ("Frage 1", "Frage 2"):
        r = await client.post(f"/api/chats/{cid}/messages", json={"content": text})
        await wait_run(services, r.json()["run_id"])
    msgs = (await client.get(f"/api/chats/{cid}")).json()["messages"]
    assert [m["content"] for m in msgs] == ["Frage 1", "A1", "Frage 2", "A2"]

    r = await client.post(f"/api/chats/{cid}/regenerate", json={})
    await wait_run(services, r.json()["run_id"])
    msgs = (await client.get(f"/api/chats/{cid}")).json()["messages"]
    assert [m["content"] for m in msgs] == ["Frage 1", "A1", "Frage 2", "A3"]

    r = await client.post(f"/api/chats/{cid}/messages", json={"content": "Frage 1b", "edit_message_id": msgs[0]["id"]})
    await wait_run(services, r.json()["run_id"])
    msgs = (await client.get(f"/api/chats/{cid}")).json()["messages"]
    assert [m["content"] for m in msgs] == ["Frage 1b", "A4"]
    turns = services.store.turns_for_conversation(cid)
    assert [(t["turn_index"], t["user_message"]) for t in turns] == [(1, "Frage 1b")]
    # Das Modell hat beim Bearbeiten nur die neue Historie gesehen
    assert [m["role"] for m in chat_calls(fake_llm)[-1]["messages"]] == ["system", "user"]


async def test_incognito_chat_is_not_persisted(services, client, fake_llm: FakeLLM):
    seen = {}

    def handler(p):
        seen["tools"] = {t["function"]["name"] for t in p.get("tools", [])}
        return {"content": "Psst."}

    fake_llm.handler = handler
    cid = await new_chat(client, incognito=True)
    assert cid.startswith("inc-")
    r = await client.post(f"/api/chats/{cid}/messages", json={"content": "Geheim"})
    await wait_run(services, r.json()["run_id"])
    assert "remember" not in seen["tools"]
    assert (await client.get(f"/api/chats/{cid}")).json()["messages"][1]["content"] == "Psst."
    assert services.store.list_conversations() == [] and services.store.list_jobs() == []
    listing = (await client.get("/api/chats")).json()
    assert listing[0]["incognito"] is True
    assert (await client.delete(f"/api/chats/{cid}")).status_code == 200


async def test_attachments(services, client, fake_llm: FakeLLM):
    fake_llm.handler = lambda p: {"content": "Gelesen."}
    cid = await new_chat(client)
    img = "data:image/png;base64,iVBORw0KGgo="
    r = await client.post(f"/api/chats/{cid}/messages", json={"content": "Schau mal", "attachments": [
        {"name": "notiz.md", "kind": "text", "text": "# Projektplan\nPhase 1"},
        {"name": "bild.png", "kind": "image", "url": img},
    ]})
    await wait_run(services, r.json()["run_id"])
    content = fake_llm.calls[0]["messages"][-1]["content"]
    assert isinstance(content, list)  # [<kontext>, Text + Datei, Bild]
    texts = " ".join(p["text"] for p in content if p["type"] == "text")
    assert '<datei name="notiz.md">' in texts and "Phase 1" in texts and "Schau mal" in texts
    assert content[-1] == {"type": "image_url", "image_url": {"url": img}}
    parts = (await client.get(f"/api/chats/{cid}")).json()["messages"][0]["parts"]
    assert [p["type"] for p in parts] == ["text", "file", "image"]
    bad = await client.post(f"/api/chats/{cid}/messages", json={"content": "x", "attachments": [
        {"name": "x.png", "kind": "image", "url": "http://evil"}]})
    assert bad.status_code == 400


async def test_openwebui_chats_appear_and_can_be_continued(services, client, fake_llm: FakeLLM):
    fake_llm.handler = lambda p: {"content": "Antwort"}
    await client.post("/v1/chat/completions", headers={"X-OpenWebUI-Chat-Id": "owui-1"},
                      json={"model": "brain/gpt-5", "messages": [{"role": "user", "content": "Aus Open WebUI"}]})
    listing = (await client.get("/api/chats")).json()
    assert listing[0]["id"] == "owui-1" and listing[0]["title"] == "Aus Open WebUI"
    chat = (await client.get("/api/chats/owui-1")).json()
    assert [m["content"] for m in chat["messages"]] == ["Aus Open WebUI", "Antwort"]
    r = await client.post("/api/chats/owui-1/messages", json={"content": "Weiter in der App"})
    await wait_run(services, r.json()["run_id"])
    assert [m["role"] for m in chat_calls(fake_llm)[-1]["messages"]] == ["system", "user", "assistant", "user"]
    assert [t["turn_index"] for t in services.store.turns_for_conversation("owui-1")] == [1, 2]

import asyncio
import json
import sys
from pathlib import Path

import pytest
from conftest import FakeLLM, sse_content, tool_call

from app.mcp.config import ConfigError, load_config
from app.services import Services
from app.tools.base import ToolContext
from app.tools.mcp import exposed_name
from app.tools.permissions import classify_reply, parse_mode_command

SERVER = str(Path(__file__).parent / "mcp_fixtures" / "notes_server.py")


def write_config(settings, tmp_path, extra: dict | None = None, **server_opts):
    notes = tmp_path / "notes.json"
    cfg = {"mcpServers": {"notes": {
        "command": sys.executable, "args": [SERVER],
        "env": {"NOTES_FILE": str(notes), "NOTES_TOKEN": "${NOTES_TOKEN}"}, **server_opts}}}
    if extra:
        cfg["mcpServers"].update(extra)
    Path(settings.mcp_config).write_text(json.dumps(cfg))
    return notes


@pytest.fixture
async def mcp_services(settings, fake_llm, tmp_path):
    notes = write_config(settings, tmp_path)
    Path(settings.mcp_secrets).write_text('NOTES_TOKEN="geheim-123"\n')
    svc = Services(settings, llm=fake_llm)
    await svc.startup()
    svc.notes_file = notes
    yield svc
    await svc.shutdown()


@pytest.fixture
async def mcp_client(mcp_services):
    import httpx

    from app.main import create_app

    app = create_app(services=mcp_services)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------- Unit-Tests
def test_exposed_name_is_valid_and_bounded():
    assert exposed_name("notes", "add_note") == "mcp__notes__add_note"
    long = exposed_name("ein.sehr-langer.server", "x" * 80)
    assert len(long) <= 64 and long.startswith("mcp__ein_sehr-langer_server__")


def test_config_parsing_and_variables(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps({"mcpServers": {
        "a": {"command": "~/bin/x", "args": ["${HOME_X:-fallback}"], "env": {"T": "${TOKEN}"},
              "tools": {"deny": ["rm*"], "readOnly": ["get_*"]}},
        "b": {"type": "streamable-http", "url": "https://example.com/mcp", "headers": {"Authorization": "Bearer ${TOKEN}"}},
        "c": {"command": "y", "disabled": True},
    }}))
    (tmp_path / "s.env").write_text("export TOKEN='abc'\n# Kommentar\n")
    cfgs = load_config(p, tmp_path / "s.env")
    assert cfgs["a"].args == ["fallback"] and cfgs["a"].env == {"T": "abc"}
    assert not cfgs["a"].command.startswith("~")
    assert cfgs["a"].rules.policy("rm_rf") == "deny" and cfgs["a"].rules.is_read_only("get_file")
    assert cfgs["b"].transport == "http" and cfgs["b"].headers["Authorization"] == "Bearer abc"
    assert cfgs["c"].enabled is False
    p.write_text(json.dumps({"mcpServers": {"x": {"args": []}}}))
    with pytest.raises(ConfigError):
        load_config(p)


def test_mode_commands_and_replies():
    assert parse_mode_command("/plan Bau mir eine Strategie") == ("plan", "Bau mir eine Strategie")
    assert parse_mode_command("/AUTO") == ("auto", "")
    assert parse_mode_command("Was ist /plan?") == (None, "Was ist /plan?")
    for yes in ["ok", "Ja bitte!", "mach das", "go", "ja, mach", "👍", "passt"]:
        assert classify_reply(yes) == "yes", yes
    for no in ["nein", "Nein, lass das", "stopp", "abbrechen"]:
        assert classify_reply(no) == "no", no
    for other in ["ja, aber ändere vorher den Titel", "Was meinst du?", "ok, und was ist mit Projekt X?"]:
        assert classify_reply(other) == "other", other


# --------------------------------------------------------- Integration (stdio)
async def test_server_starts_and_tools_are_classified(mcp_services):
    conn = mcp_services.mcp.connections["notes"]
    assert conn.status == "ready", conn.error
    assert "notes server starting" in conn.log_tail()
    ts = mcp_services.tools.resolve(ToolContext("c", permission_mode="ask"))
    assert ts.tools["mcp__notes__list_notes"].decision == "allow"  # readOnlyHint
    assert ts.tools["mcp__notes__add_note"].decision == "ask"
    assert ts.tools["search_memory"].decision == "allow"
    plan = mcp_services.tools.resolve(ToolContext("c", permission_mode="plan"))
    assert "mcp__notes__add_note" not in plan.tools and "mcp__notes__list_notes" in plan.tools
    assert "remember" in plan.tools  # Gedächtnis darf immer
    auto = mcp_services.tools.resolve(ToolContext("c", permission_mode="auto"))
    assert auto.tools["mcp__notes__add_note"].decision == "allow"
    system, _ = mcp_services.prompt_builder.system_prompt(None, tools_section=mcp_services.mcp_instructions())
    assert "**notes** (4 Tools): Verwaltet Notizen" in system


async def test_secrets_are_expanded(mcp_services):
    res = await mcp_services.mcp.connections["notes"].call_tool("secret_token", {})
    assert res.content[0].text == "geheim-123"


async def test_ask_mode_requires_approval_then_executes(mcp_services, mcp_client, fake_llm: FakeLLM):
    calls = {"n": 0}

    def handler(payload):
        calls["n"] += 1
        last = payload["messages"][-1]
        if calls["n"] == 1:
            return {"content": "", "tool_calls": [tool_call("mcp__notes__add_note", {"text": "Milch kaufen"})]}
        if calls["n"] == 2:
            assert last["role"] == "tool" and "Freigabe #" in last["content"]
            return {"content": "Soll ich die Notiz „Milch kaufen“ anlegen?"}
        # Runde 2: Nach „ok“ steht das Ergebnis im Kontext
        assert "freigegeben" in last["content"] and "Notiz gespeichert" in last["content"]
        return {"content": "Erledigt!"}

    fake_llm.handler = handler
    h = {"X-OpenWebUI-Chat-Id": "chat-a"}
    r = await mcp_client.post("/v1/chat/completions", headers=h, json={
        "model": "brain/gpt-5", "stream": True, "messages": [{"role": "user", "content": "Notiere Milch kaufen"}]})
    text = sse_content(r.text)
    assert "⏸️ Wartet auf Freigabe" in text
    assert not mcp_services.notes_file.exists()  # NICHT ausgeführt
    assert len(mcp_services.store.pending_approvals("chat-a")) == 1

    r = await mcp_client.post("/v1/chat/completions", headers=h, json={"model": "brain/gpt-5", "stream": True, "messages": [
        {"role": "user", "content": "Notiere Milch kaufen"},
        {"role": "assistant", "content": text},
        {"role": "user", "content": "ok"},
    ]})
    text2 = sse_content(r.text)
    assert "✅ Freigegeben" in text2 and text2.endswith("Erledigt!")
    assert json.loads(mcp_services.notes_file.read_text()) == ["Milch kaufen"]
    assert mcp_services.store.pending_approvals("chat-a") == []


async def test_rejection_and_expiry(mcp_services, mcp_client, fake_llm: FakeLLM):
    store = mcp_services.store
    store.create_approval("chat-b", "mcp__notes__add_note", {"text": "x"}, "x")
    fake_llm.handler = lambda p: {"content": "Ok, lasse ich."}
    await mcp_client.post("/v1/chat/completions", headers={"X-OpenWebUI-Chat-Id": "chat-b"}, json={
        "model": "brain/gpt-5", "messages": [{"role": "user", "content": "nein"}]})
    assert store.pending_approvals("chat-b") == [] and not mcp_services.notes_file.exists()

    aid = store.create_approval("chat-c", "mcp__notes__add_note", {"text": "y"}, "y")
    await mcp_client.post("/v1/chat/completions", headers={"X-OpenWebUI-Chat-Id": "chat-c"}, json={
        "model": "brain/gpt-5", "messages": [{"role": "user", "content": "Lass uns über was anderes reden"}]})
    assert store.get_approval(aid)["status"] == "expired"


async def test_auto_mode_command_executes_directly_and_is_remembered(mcp_services, mcp_client, fake_llm: FakeLLM):
    seen = []

    def handler(payload):
        seen.append(payload)
        if payload["messages"][-1]["role"] == "user":
            return {"content": "", "tool_calls": [tool_call("mcp__notes__add_note", {"text": "Auto"})]}
        return {"content": "Fertig."}

    fake_llm.handler = handler
    h = {"X-OpenWebUI-Chat-Id": "chat-auto"}
    r = await mcp_client.post("/v1/chat/completions", headers=h, json={
        "model": "brain/gpt-5", "messages": [{"role": "user", "content": "/auto Notiere Auto"}]})
    assert r.json()["choices"][0]["message"]["content"].endswith("Fertig.")
    assert json.loads(mcp_services.notes_file.read_text()) == ["Auto"]
    user_msg = seen[0]["messages"][-1]["content"]
    assert "/auto" not in user_msg and user_msg.endswith("Notiere Auto") and "Freigabe-Modus: auto" in user_msg
    assert mcp_services.store.get_permission_mode("chat-auto") == "auto"


async def test_plan_mode_hides_write_tools(mcp_services, mcp_client, fake_llm: FakeLLM):
    seen = {}

    def handler(payload):
        seen["tools"] = {t["function"]["name"] for t in payload.get("tools", [])}
        return {"content": "Hier ist der Plan."}

    fake_llm.handler = handler
    await mcp_client.post("/v1/chat/completions", json={
        "model": "brain/gpt-5", "messages": [{"role": "user", "content": "/plan Wie organisiere ich meine Notizen?"}]})
    assert "mcp__notes__list_notes" in seen["tools"] and "mcp__notes__add_note" not in seen["tools"]


async def test_read_tool_runs_without_approval(mcp_services, mcp_client, fake_llm: FakeLLM):
    mcp_services.notes_file.write_text(json.dumps(["Zahnarzt Dienstag"]))
    calls = {"n": 0}

    def handler(payload):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"content": "", "tool_calls": [tool_call("mcp__notes__list_notes", {})]}
        assert "Zahnarzt Dienstag" in payload["messages"][-1]["content"]
        return {"content": "Du hast: Zahnarzt Dienstag."}

    fake_llm.handler = handler
    r = await mcp_client.post("/v1/chat/completions", json={
        "model": "brain/gpt-5", "stream": True, "messages": [{"role": "user", "content": "Welche Notizen habe ich?"}]})
    assert "🔧 notes · list_notes" in sse_content(r.text)


async def test_deny_rule_and_hot_reload(mcp_services, settings, tmp_path):
    write_config(settings, tmp_path, tools={"deny": ["crash"], "ask": ["list_notes"]})
    changes = await mcp_services.mcp.reload()
    assert changes["restarted"] == []  # nur Regeln geändert → kein Neustart
    ts = mcp_services.tools.resolve(ToolContext("c", permission_mode="auto"))
    assert "mcp__notes__crash" not in ts.tools
    assert ts.tools["mcp__notes__list_notes"].decision == "ask"


async def test_crash_restarts_server(mcp_services):
    conn = mcp_services.mcp.connections["notes"]
    with pytest.raises(Exception):
        await conn.call_tool("crash", {})
    for _ in range(100):  # Owner-Task startet neu (Backoff 2 s)
        if conn.restarts >= 1 and conn.status == "ready":
            break
        await asyncio.sleep(0.1)
    assert conn.status == "ready" and conn.restarts >= 1
    res = await conn.call_tool("list_notes", {})
    assert res.content[0].text == "(keine Notizen)"


async def test_failing_server_reports_error(settings, fake_llm, tmp_path):
    write_config(settings, tmp_path, extra={"kaputt": {"command": "gibt-es-nicht-xyz"}})
    settings.mcp_startup_wait = 10
    svc = Services(settings, llm=fake_llm)
    await svc.startup()
    try:
        status = {s["name"]: s for s in svc.mcp.status()["servers"]}
        assert status["notes"]["status"] == "ready"
        assert status["kaputt"]["status"] in ("failed", "starting")
        for _ in range(50):
            if svc.mcp.connections["kaputt"].error:
                break
            await asyncio.sleep(0.1)
        assert "gibt-es-nicht-xyz" in svc.mcp.connections["kaputt"].error
    finally:
        await svc.shutdown()


async def test_tool_search_when_many_tools(mcp_services, mcp_client, fake_llm: FakeLLM, settings):
    mcp_services.tools.search_threshold = 3
    seen = []

    def handler(payload):
        seen.append({t["function"]["name"] for t in payload.get("tools", [])})
        n = len(seen)
        if n == 1:
            return {"content": "", "tool_calls": [tool_call("tool_search", {"query": "Notizen auflisten"})]}
        if n == 2:
            return {"content": "", "tool_calls": [tool_call("mcp__notes__list_notes", {}, "call_2")]}
        return {"content": "Keine Notizen."}

    fake_llm.handler = handler
    await mcp_client.post("/v1/chat/completions", json={
        "model": "brain/gpt-5", "messages": [{"role": "user", "content": "Zeig meine Notizen"}]})
    assert "tool_search" in seen[0] and "mcp__notes__list_notes" not in seen[0]
    assert "mcp__notes__list_notes" in seen[1]


async def test_admin_endpoints(mcp_services, mcp_client):
    r = await mcp_client.get("/mcp/servers")
    assert r.json()["servers"][0]["status"] == "ready"
    r = await mcp_client.post("/mcp/servers/notes/tools/add_note", json={"text": "Admin"})
    assert r.json()["text"].startswith("Notiz gespeichert")
    r = await mcp_client.get("/tools", params={"mode": "plan"})
    names = {t["name"] for t in r.json()["tools"]}
    assert "mcp__notes__list_notes" in names and "mcp__notes__add_note" not in names
    r = await mcp_client.get("/mcp/servers/notes/logs")
    assert "notes server starting" in r.json()["log"]


async def test_streamable_http_server(settings, fake_llm, tmp_path):
    import socket
    import subprocess

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    notes = tmp_path / "http-notes.json"
    proc = subprocess.Popen([sys.executable, SERVER, "--http", str(port)], env={"NOTES_FILE": str(notes), "PATH": ""},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                await asyncio.sleep(0.1)
        Path(settings.mcp_config).write_text(json.dumps({"mcpServers": {
            "remote": {"type": "http", "url": f"http://127.0.0.1:{port}/mcp", "headers": {"X-Test": "1"}}}}))
        svc = Services(settings, llm=fake_llm)
        await svc.startup()
        try:
            conn = svc.mcp.connections["remote"]
            assert conn.status == "ready", conn.error
            ts = svc.tools.resolve(ToolContext("c", permission_mode="auto"))
            res = await ts.tools["mcp__remote__add_note"].provider.call("mcp__remote__add_note", {"text": "per HTTP"}, ToolContext("c"))
            assert res.text.startswith("Notiz gespeichert"), res.text
            assert json.loads(notes.read_text()) == ["per HTTP"]
        finally:
            await svc.shutdown()
    finally:
        proc.terminate()
        proc.wait(timeout=10)

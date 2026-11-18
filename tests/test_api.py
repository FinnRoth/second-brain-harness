import json

from conftest import FakeLLM, parse_sse, sse_content, tool_call


async def test_models(client):
    r = await client.get("/v1/models")
    ids = [m["id"] for m in r.json()["data"]]
    assert ids == ["brain/gpt-5", "incognito/gpt-5"]


async def test_auth(services, client):
    services.settings.harness_api_key = "secret"
    try:
        assert (await client.get("/v1/models")).status_code == 401
        r = await client.get("/v1/models", headers={"Authorization": "Bearer secret"})
        assert r.status_code == 200
    finally:
        services.settings.harness_api_key = ""


async def test_streaming_injects_memory_and_stores_turn(services, client, fake_llm: FakeLLM):
    store = services.store
    store.add_fact("Alex arbeitet als Data Engineer bei Contoso", [store.self_id()], "arbeit", 5)
    seen = {}

    def handler(payload):
        seen["payload"] = payload
        return {"content": "Hallo Alex! Wie läuft es bei Contoso?"}

    fake_llm.handler = handler
    body = {"model": "brain/gpt-5", "stream": True,
            "messages": [{"role": "user", "content": "Hi, wie läuft es so bei der Arbeit bei Contoso?"}]}
    r = await client.post("/v1/chat/completions", json=body, headers={"X-OpenWebUI-Chat-Id": "chat-1"})
    assert r.status_code == 200
    assert sse_content(r.text) == "Hallo Alex! Wie läuft es bei Contoso?"
    chunks = parse_sse(r.text)
    assert all(c["model"] == "brain/gpt-5" for c in chunks)
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"

    sent = seen["payload"]
    assert sent["model"] == "gpt-5"
    assert "Data Engineer bei Contoso" in sent["messages"][0]["content"]
    assert {t["function"]["name"] for t in sent["tools"]} >= {"search_memory", "remember"}

    turns = store.turns_for_conversation("chat-1")
    assert turns[0]["assistant_message"] == "Hallo Alex! Wie läuft es bei Contoso?"
    assert [j["type"] for j in store.list_jobs(status="pending")] == ["summarize_conversation", "extract_turn"]


async def test_tool_loop_streaming(services, client, fake_llm: FakeLLM):
    store = services.store
    max_id = store.create_entity("person", "Max Müller", ["Max"], "CTO bei Acme")
    store.add_fact("Max Müller interessiert sich für Segeln", [max_id])
    calls = {"n": 0}

    def handler(payload):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"content": "", "tool_calls": [tool_call("get_entity", {"name": "Max"})]}
        tool_msg = payload["messages"][-1]
        assert tool_msg["role"] == "tool" and "Segeln" in tool_msg["content"]
        return {"content": "Max mag Segeln."}

    fake_llm.handler = handler
    body = {"model": "brain/gpt-5", "stream": True,
            "messages": [{"role": "user", "content": "Was weißt du über Max?"}]}
    r = await client.post("/v1/chat/completions", json=body, headers={"X-OpenWebUI-Chat-Id": "chat-2"})
    text = sse_content(r.text)
    assert '<details type="memory">' in text and text.endswith("Max mag Segeln.")
    assert not any(c["choices"][0]["delta"].get("tool_calls") for c in parse_sse(r.text) if c.get("choices"))
    turn = store.turns_for_conversation("chat-2")[0]
    assert json.loads(turn["tool_log"])[0]["name"] == "get_entity"
    assert turn["assistant_message"] == "Max mag Segeln."  # Aktivitätsblock wird nicht gespeichert


async def test_remember_tool_non_streaming(services, client, fake_llm: FakeLLM):
    calls = {"n": 0}

    def handler(payload):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"content": None, "tool_calls": [tool_call("remember", {
                "content": "Alex will 2027 einen Marathon laufen",
                "entities": [{"name": "Marathon 2027", "type": "goal"}], "importance": 5})]}
        return {"content": "Notiert!"}

    fake_llm.handler = handler
    r = await client.post("/v1/chat/completions", json={
        "model": "brain/gpt-5", "messages": [{"role": "user", "content": "Merk dir: Ich will 2027 Marathon laufen"}]})
    content = r.json()["choices"][0]["message"]["content"]
    assert content.endswith("Notiert!")
    goal = services.store.find_entity_by_name("Marathon 2027")
    assert goal["type"] == "goal"
    assert services.store.list_facts(entity_id=goal["id"])[0]["importance"] == 5


async def test_incognito_does_not_store_or_write(services, client, fake_llm: FakeLLM):
    seen = {}

    def handler(payload):
        seen["tools"] = {t["function"]["name"] for t in payload.get("tools", [])}
        return {"content": "Psst."}

    fake_llm.handler = handler
    await client.post("/v1/chat/completions", json={
        "model": "incognito/gpt-5", "stream": True, "messages": [{"role": "user", "content": "Geheim"}]})
    assert "remember" not in seen["tools"] and "search_memory" in seen["tools"]
    assert services.store.list_conversations() == []
    assert services.store.list_jobs() == []


async def test_openwebui_task_is_passed_through(services, client, fake_llm: FakeLLM):
    seen = {}

    def handler(payload):
        seen["payload"] = payload
        return {"content": '{"title": "Test"}'}

    fake_llm.handler = handler
    task = "### Task:\nGenerate a concise title...\n### Guidelines:\n- x\n### Output:\nJSON\n<chat_history>\nUSER: hi\n</chat_history>"
    r = await client.post("/v1/chat/completions", json={
        "model": "brain/gpt-5", "stream": False, "messages": [{"role": "user", "content": task}]})
    assert r.json()["choices"][0]["message"]["content"] == '{"title": "Test"}'
    assert seen["payload"]["model"] == "gpt-5"
    assert "tools" not in seen["payload"]
    assert len(seen["payload"]["messages"]) == 1  # kein Gedächtnis-Systemprompt
    assert services.store.list_conversations() == []


async def test_activity_blocks_are_stripped_from_history(services, client, fake_llm: FakeLLM):
    seen = {}

    def handler(payload):
        seen["messages"] = payload["messages"]
        return {"content": "ok"}

    fake_llm.handler = handler
    await client.post("/v1/chat/completions", json={"model": "brain/gpt-5", "messages": [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": '<details type="memory">\n<summary>x</summary>\n</details>\n\nAntwort'},
        {"role": "user", "content": "b"},
    ]})
    assert seen["messages"][2]["content"] == "Antwort"


async def test_upstream_error_is_reported_in_stream(services, client, fake_llm: FakeLLM):
    from app.llm import UpstreamError

    async def broken(payload):
        raise UpstreamError(500, "kaputt")
        yield  # pragma: no cover

    fake_llm.chat_stream = broken
    r = await client.post("/v1/chat/completions", json={
        "model": "brain/gpt-5", "stream": True, "messages": [{"role": "user", "content": "hi"}]})
    assert "kaputt" in sse_content(r.text)
    assert r.text.rstrip().endswith("data: [DONE]")
    assert services.store.list_conversations() == []


async def test_memory_admin_endpoints(services, client):
    r = await client.post("/memory/entities", json={"type": "project", "name": "Blog", "summary": "Tech-Blog"})
    pid = r.json()["id"]
    r = await client.post("/memory/facts", json={"content": "Der Blog läuft auf Hugo", "entity_ids": [pid]})
    fid = r.json()["id"]
    r = await client.patch(f"/memory/facts/{fid}", json={"content": "Der Blog läuft auf Astro"})
    new = r.json()
    assert new["id"] != fid and new["content"] == "Der Blog läuft auf Astro"
    detail = (await client.get(f"/memory/entities/{pid}")).json()
    assert {f["status"] for f in detail["facts"]} == {"active", "superseded"}
    hits = (await client.get("/memory/search", params={"q": "Was ist mit dem Blog?"})).json()
    assert hits["detected_entities"][0]["name"] == "Blog"
    assert (await client.get("/memory/stats")).json()["facts"]["active"] == 1


def _conflicting_upstream(fake_llm: FakeLLM, seen: list):
    """Simuliert ein Modell, das Tools + reasoning_effort ablehnt."""
    from app.llm import UpstreamError

    orig_chat, orig_stream = fake_llm.chat, fake_llm.chat_stream
    msg = ("Function tools with reasoning_effort are not supported for gpt-6.1 in /v1/chat/completions. "
           "To use function tools, use /v1/responses or set reasoning_effort to 'none'.")

    def check(payload):
        seen.append(payload)
        if payload.get("tools") and payload.get("reasoning_effort") not in (None, "none"):
            raise UpstreamError(400, msg)

    async def chat(payload):
        check(payload)
        return await orig_chat(payload)

    async def chat_stream(payload):
        check(payload)
        async for c in orig_stream(payload):
            yield c

    fake_llm.chat, fake_llm.chat_stream = chat, chat_stream


async def test_reasoning_tool_conflict_drops_tools(services, client, fake_llm: FakeLLM):
    seen: list = []
    _conflicting_upstream(fake_llm, seen)
    fake_llm.handler = lambda p: {"content": "Antwort mit Reasoning"}
    body = {"model": "brain/gpt-5", "stream": True, "reasoning_effort": "high",
            "messages": [{"role": "user", "content": "hi"}]}
    r = await client.post("/v1/chat/completions", json=body)
    assert sse_content(r.text) == "Antwort mit Reasoning"
    assert len(seen) == 2 and "tools" not in seen[1] and seen[1]["reasoning_effort"] == "high"
    assert "keine Gedächtnis-Tools" in seen[1]["messages"][0]["content"]
    assert services.store.list_conversations()  # Turn wurde trotzdem gespeichert

    # Zweite Anfrage: Konflikt ist bekannt → kein fehlschlagender Versuch mehr
    seen.clear()
    r = await client.post("/v1/chat/completions", json={**body, "stream": False})
    assert r.json()["choices"][0]["message"]["content"] == "Antwort mit Reasoning"
    assert len(seen) == 1 and "tools" not in seen[0]


async def test_reasoning_tool_conflict_prefer_tools(services, client, fake_llm: FakeLLM):
    services.settings.reasoning_tools_conflict = "prefer_tools"
    seen: list = []
    _conflicting_upstream(fake_llm, seen)
    fake_llm.handler = lambda p: {"content": "ok"}
    r = await client.post("/v1/chat/completions", json={
        "model": "brain/gpt-5", "reasoning_effort": "medium", "messages": [{"role": "user", "content": "hi"}]})
    assert r.json()["choices"][0]["message"]["content"] == "ok"
    assert seen[-1]["tools"] and seen[-1]["reasoning_effort"] == "none"

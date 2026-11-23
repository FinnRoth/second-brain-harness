import asyncio
import json

from conftest import FakeLLM, tool_call

from app.agent.runs import assistant_history
from app.usage import normalize_usage, summarize_calls


def test_assistant_history_replays_tools_and_reasoning():
    m = {"id": 7, "role": "assistant", "status": "done", "content": "Vorab. Fertig.", "parts": [
        {"type": "reasoning", "text": "Ich brauche die Notizen, weil der Nutzer nach Fristen fragt."},
        {"type": "text", "text": "Vorab. "},
        {"type": "tool", "call_id": "c1", "name": "mcp__notes__list_notes", "args": {}, "status": "done", "result": "x" * 3000},
        {"type": "tool", "call_id": "c2", "name": "mcp__notes__add_note", "args": {"text": "A"}, "status": "denied"},
        {"type": "reasoning", "text": "Jetzt antworten."},
        {"type": "text", "text": "Fertig."},
    ]}
    out = assistant_history(m, max_result_chars=100)
    assert [x["role"] for x in out] == ["assistant", "tool", "tool", "assistant"]
    first = out[0]
    assert first["content"].startswith("Vorab.") and "weil der Nutzer nach Fristen fragt" in first["content"]
    assert [c["id"] for c in first["tool_calls"]] == ["h7_c1", "h7_c2"]
    assert json.loads(first["tool_calls"][1]["function"]["arguments"]) == {"text": "A"}
    assert out[1]["content"].startswith("x" * 100) and "gekürzt" in out[1]["content"]
    assert "abgelehnt" in out[2]["content"]
    assert out[3] == {"role": "assistant", "content": "Fertig."}
    plain = {"id": 1, "role": "assistant", "status": "done", "content": "Hi", "parts": [{"type": "text", "text": "Hi"}]}
    assert assistant_history(plain) == [{"role": "assistant", "content": "Hi"}]


def test_usage_normalization_and_summary():
    u = {"prompt_tokens": 1000, "completion_tokens": 200, "prompt_tokens_details": {"cached_tokens": 600},
         "completion_tokens_details": {"reasoning_tokens": 50}}
    assert normalize_usage(u) == {"prompt_tokens": 1000, "completion_tokens": 200, "cached_tokens": 600, "reasoning_tokens": 50}
    s = summarize_calls([u, {"input_tokens": 1300, "output_tokens": 100}])
    assert s["prompt_tokens"] == 2300 and s["completion_tokens"] == 300 and s["calls"] == 2
    assert s["context_tokens"] == 1400


async def test_catalog_from_litellm_with_overrides_and_cost(services, fake_llm: FakeLLM):
    fake_llm.model_info_rows = [{"model_name": "gpt-5", "model_info": {
        "max_input_tokens": 272000, "input_cost_per_token": 1.25e-6, "output_cost_per_token": 1e-5,
        "cache_read_input_token_cost": 1.25e-7}}]
    cat = services.catalog
    await cat.refresh(force=True)
    info = cat.info("gpt-5")
    assert info["context_window"] == 272000 and info["source"]["input_cost_per_mtok"] == "litellm"
    cost = cat.cost("gpt-5", {"prompt_tokens": 1_000_000, "completion_tokens": 100_000, "cached_tokens": 400_000})
    assert round(cost, 4) == round(0.6 * 1.25 + 0.4 * 0.125 + 0.1 * 10, 4)
    cat.set_overrides("gpt-5", {"context_window": 400000, "input_cost_per_mtok": 2})
    info = cat.info("gpt-5")
    assert info["context_window"] == 400000 and info["source"]["context_window"] == "manuell"
    assert cat.cost("unbekannt", {"prompt_tokens": 10, "completion_tokens": 1}) is None


async def test_chat_usage_is_tracked_and_shown(services, client, fake_llm: FakeLLM):
    fake_llm.model_info_rows = [{"model_name": "gpt-5", "model_info": {
        "max_input_tokens": 100000, "input_cost_per_token": 1e-6, "output_cost_per_token": 2e-6}}]
    await services.catalog.refresh(force=True)
    orig = fake_llm.chat_stream

    async def with_usage(payload):
        async for c in orig(payload):
            yield c
        yield {"choices": [], "usage": {"prompt_tokens": 1200, "completion_tokens": 300}}

    fake_llm.chat_stream = with_usage
    calls = {"n": 0}

    def handler(p):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"content": "", "tool_calls": [tool_call("search_memory", {"query": "x"})]}
        # Verlauf enthält den früheren Tool-Aufruf ab der zweiten Nachricht
        return {"content": "Antwort"}

    fake_llm.handler = handler
    cid = (await client.post("/api/chats", json={})).json()["id"]
    r = await client.post(f"/api/chats/{cid}/messages", json={"content": "Frage"})
    await asyncio.wait_for(services.runs.runs[r.json()["run_id"]].task, 10)
    chat = (await client.get(f"/api/chats/{cid}")).json()
    usage = chat["messages"][1]["usage"]
    assert usage["prompt_tokens"] == 2400 and usage["completion_tokens"] == 600 and usage["calls"] == 2
    assert usage["cost"] == round(2400 * 1e-6 + 600 * 2e-6, 6)
    assert chat["usage"]["total_tokens"] == 3000 and chat["usage"]["context_tokens"] == 1500
    assert chat["usage"]["context_window"] == 100000

    # Zweite Nachricht: früherer Tool-Aufruf steht im Verlauf
    r = await client.post(f"/api/chats/{cid}/messages", json={"content": "Warum hast du gesucht?"})
    await asyncio.wait_for(services.runs.runs[r.json()["run_id"]].task, 10)
    sent = [c for c in fake_llm.calls if "Chat-Titel" not in c["messages"][0]["content"]][-1]["messages"]
    roles = [m["role"] for m in sent]
    assert roles[1:5] == ["user", "assistant", "tool", "assistant"]
    assert sent[2]["tool_calls"][0]["function"]["name"] == "search_memory"

    summary = (await client.get("/api/usage?days=7")).json()
    kinds = {k["kind"]: k for k in summary["by_kind"]}
    assert kinds["chat"]["prompt_tokens"] == 3600  # 2 Calls + 1 Call
    models = (await client.get("/api/models")).json()
    assert models[0]["model"] == "gpt-5" and models[0]["context_window"] == 100000
    r = await client.put("/api/models/gpt-5", json={"context_window": 200000})
    assert r.json()["context_window"] == 200000

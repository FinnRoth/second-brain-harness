import json

from conftest import FakeLLM, parse_sse, sse_content, tool_call

from app.llm import UpstreamError
from app.responses_api import RAW_KEY, from_response, to_responses_request


def test_request_translation():
    req = to_responses_request(
        {
            "model": "gpt-x",
            "messages": [
                {"role": "system", "content": "Sys"},
                {"role": "user", "content": [{"type": "text", "text": "Was ist das?"},
                                             {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]},
                {"role": "assistant", "content": None, "tool_calls": [tool_call("search_memory", {"query": "x"})]},
                {"role": "tool", "tool_call_id": "call_1", "content": "Ergebnis"},
            ],
            "tools": [{"type": "function", "function": {"name": "search_memory", "description": "d",
                                                        "parameters": {"type": "object"}}}],
            "reasoning_effort": "high",
            "max_tokens": 500,
            "response_format": {"type": "json_object"},
            "seed": 1,
        },
        reasoning_summary="auto",
    )
    assert req["store"] is False
    assert req["input"][0] == {"role": "system", "content": "Sys"}
    assert req["input"][1]["content"][1] == {"type": "input_image", "image_url": "data:image/png;base64,AA"}
    assert req["input"][2] == {"type": "function_call", "call_id": "call_1", "name": "search_memory",
                               "arguments": json.dumps({"query": "x"})}
    assert req["input"][3] == {"type": "function_call_output", "call_id": "call_1", "output": "Ergebnis"}
    assert req["tools"] == [{"type": "function", "name": "search_memory", "description": "d",
                             "parameters": {"type": "object"}}]
    assert req["reasoning"] == {"effort": "high", "summary": "auto"}
    assert req["include"] == ["reasoning.encrypted_content"]
    assert req["max_output_tokens"] == 500
    assert req["text"] == {"format": {"type": "json_object"}}
    assert "seed" not in req


def test_response_translation_without_encrypted_reasoning_drops_raw():
    out = from_response({"status": "completed", "output": [
        {"id": "rs_1", "type": "reasoning", "summary": [{"type": "summary_text", "text": "Hm"}]},
        {"id": "fc_1", "type": "function_call", "call_id": "c1", "name": "f", "arguments": "{}"},
    ], "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}})
    msg = out["choices"][0]["message"]
    assert msg["reasoning_content"] == "Hm"
    assert msg["tool_calls"][0]["id"] == "c1"
    assert out["choices"][0]["finish_reason"] == "tool_calls"
    assert out["usage"]["completion_tokens"] == 2
    assert RAW_KEY not in out  # ohne encrypted_content nicht zurückreichbar


async def test_forced_responses_streaming_with_tools_and_reasoning(services, client, fake_llm: FakeLLM):
    services.settings.upstream_api = "responses"
    store = services.store
    eid = store.create_entity("person", "Max Müller", ["Max"], "CTO bei Acme")
    store.add_fact("Max Müller segelt gern", [eid])

    def handler(payload):
        if not any(i.get("type") == "function_call_output" for i in payload["input"]):
            return {"reasoning": "Ich schaue im Gedächtnis nach.", "tool_calls": [tool_call("get_entity", {"name": "Max"})]}
        return {"reasoning": "Jetzt weiß ich es.", "content": "Max segelt gern."}

    fake_llm.responses_handler = handler
    r = await client.post("/v1/chat/completions", headers={"X-OpenWebUI-Chat-Id": "r1"}, json={
        "model": "brain/gpt-5", "stream": True, "reasoning_effort": "high",
        "messages": [{"role": "user", "content": "Was macht Max in der Freizeit?"}]})

    chunks = parse_sse(r.text)
    reasoning = "".join(c["choices"][0]["delta"].get("reasoning_content") or "" for c in chunks if c.get("choices"))
    assert "Ich schaue im Gedächtnis nach." in reasoning and "Jetzt weiß ich es." in reasoning
    assert sse_content(r.text).endswith("Max segelt gern.")
    assert chunks[-1]["usage"]["prompt_tokens"] == 20  # beide Calls des Tool-Loops
    assert chunks[-1]["usage"]["context_tokens"] == 15  # letzter Prompt + Antwort
    assert not fake_llm.calls  # nichts lief über /chat/completions

    first, second = fake_llm.responses_calls
    assert first["reasoning"]["effort"] == "high" and first["store"] is False
    assert {t["name"] for t in first["tools"]} >= {"search_memory", "get_entity"}
    # Zweiter Schritt bekommt Reasoning-Item (verschlüsselt) + Function-Call + Ergebnis zurück
    types = [i.get("type") for i in second["input"]]
    assert types[-3:] == ["reasoning", "function_call", "function_call_output"]
    assert second["input"][-3]["encrypted_content"] == "ENC1"
    assert "Segelt gern".lower() in second["input"][-1]["output"].lower()
    assert store.turns_for_conversation("r1")[0]["assistant_message"] == "Max segelt gern."


async def test_auto_switch_to_responses_on_conflict(services, client, fake_llm: FakeLLM):

    async def conflicting_stream(payload):
        raise UpstreamError(400, "Function tools with reasoning_effort are not supported ... use /v1/responses")
        yield  # pragma: no cover

    fake_llm.chat_stream = conflicting_stream
    fake_llm.responses_handler = lambda p: {"reasoning": "", "content": "Über Responses!"}
    body = {"model": "brain/gpt-5", "stream": True, "reasoning_effort": "medium",
            "messages": [{"role": "user", "content": "hi"}]}
    r = await client.post("/v1/chat/completions", json=body)
    assert sse_content(r.text) == "Über Responses!"
    assert len(fake_llm.responses_calls) == 1 and fake_llm.responses_calls[0]["tools"]
    assert "gpt-5" in services.responses_models

    # Folgeanfrage geht direkt an /v1/responses
    r = await client.post("/v1/chat/completions", json={**body, "stream": False})
    assert r.json()["choices"][0]["message"]["content"] == "Über Responses!"
    assert RAW_KEY not in r.json()
    assert len(fake_llm.responses_calls) == 2


async def test_responses_unsupported_falls_back_to_chat_without_tools(services, client, fake_llm: FakeLLM):
    seen = []
    orig = fake_llm.chat_stream

    async def chat_stream(payload):
        seen.append(payload)
        if payload.get("tools"):
            raise UpstreamError(400, "Function tools with reasoning_effort are not supported")
        async for c in orig(payload):
            yield c

    async def no_responses(payload):
        raise UpstreamError(404, "Not Found: /v1/responses")
        yield  # pragma: no cover

    fake_llm.chat_stream = chat_stream
    fake_llm.responses_stream = no_responses
    fake_llm.handler = lambda p: {"content": "Fallback"}
    r = await client.post("/v1/chat/completions", json={
        "model": "brain/gpt-5", "stream": True, "reasoning_effort": "high",
        "messages": [{"role": "user", "content": "hi"}]})
    assert sse_content(r.text) == "Fallback"
    assert "tools" not in seen[-1] and seen[-1]["reasoning_effort"] == "high"
    assert "gpt-5" in services.responses_unsupported


async def test_summary_rejected_is_disabled(services, client, fake_llm: FakeLLM):
    services.settings.upstream_api = "responses"
    orig = fake_llm.responses_stream

    async def responses_stream(payload):
        if "summary" in (payload.get("reasoning") or {}):
            raise UpstreamError(400, "reasoning.summary is not supported for this model")
        async for ev in orig(payload):
            yield ev

    fake_llm.responses_stream = responses_stream
    fake_llm.responses_handler = lambda p: {"content": "ok"}
    r = await client.post("/v1/chat/completions", json={
        "model": "brain/gpt-5", "reasoning_effort": "low", "messages": [{"role": "user", "content": "hi"}]})
    assert r.json()["choices"][0]["message"]["content"] == "ok"
    assert services.responses.summary == ""

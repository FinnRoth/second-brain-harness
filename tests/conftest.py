from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable

import httpx
import numpy as np
import pytest

from app.config import Settings
from app.main import create_app
from app.services import Services


class FakeLLM:
    """Simuliert LiteLLM. `handler(payload) -> message-dict` entscheidet über die Antwort."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses_calls: list[dict[str, Any]] = []
        self.model_info_rows: list[dict[str, Any]] = []
        self.responses_handler: Callable[[dict[str, Any]], dict[str, Any]] = lambda p: {"content": "OK"}
        self.handler: Callable[[dict[str, Any]], dict[str, Any]] = lambda p: {"content": "OK"}
        self.background: Callable[[dict[str, Any]], dict[str, Any]] = lambda p: {
            "content": json.dumps({"entities": [], "facts": [], "relations": []})
        }

    def _respond(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(payload)
        system = payload["messages"][0]["content"] if payload["messages"] else ""
        if "Chat-Titel" in system:
            return {"content": "Testtitel"}
        if "Gedächtnis-Modul" in system or "Du pflegst die Memory-Zusammenfassung" in system or "zusammen, damit" in system:
            return self.background(payload)
        return self.handler(payload)

    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        msg = {"role": "assistant", **self._respond(payload)}
        return {
            "id": "x",
            "object": "chat.completion",
            "model": payload["model"],
            "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if msg.get("tool_calls") else "stop"}],
        }

    async def chat_stream(self, payload: dict[str, Any]):
        msg = self._respond(payload)
        base = {"id": "x", "object": "chat.completion.chunk", "model": payload["model"]}
        yield {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]}
        content = msg.get("content") or ""
        for piece in re.findall(r".{1,5}", content, re.DOTALL):
            yield {**base, "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
        for i, tc in enumerate(msg.get("tool_calls") or []):
            args = tc["function"]["arguments"]
            half = len(args) // 2
            yield {
                **base,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"index": i, "id": tc["id"], "type": "function",
                                 "function": {"name": tc["function"]["name"], "arguments": args[:half]}}
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            }
            yield {
                **base,
                "choices": [
                    {"index": 0, "delta": {"tool_calls": [{"index": i, "function": {"arguments": args[half:]}}]},
                     "finish_reason": None}
                ],
            }
        yield {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if msg.get("tool_calls") else "stop"}]}

    # ------------------------------------------------------------ Responses API
    def _responses_output(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.responses_calls.append(payload)
        msg = self.responses_handler(payload)
        output = []
        if msg.get("reasoning") is not None:
            item = {"id": f"rs_{len(self.responses_calls)}", "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": msg["reasoning"]}] if msg["reasoning"] else []}
            if "reasoning.encrypted_content" in (payload.get("include") or []):
                item["encrypted_content"] = f"ENC{len(self.responses_calls)}"
            output.append(item)
        for tc in msg.get("tool_calls") or []:
            output.append({"id": "fc_" + tc["id"], "type": "function_call", "status": "completed",
                           "call_id": tc["id"], "name": tc["function"]["name"],
                           "arguments": tc["function"]["arguments"]})
        if msg.get("content"):
            output.append({"id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text", "text": msg["content"], "annotations": []}]})
        return {"id": "resp_1", "object": "response", "status": "completed", "model": payload["model"],
                "output": output, "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                                            "output_tokens_details": {"reasoning_tokens": 3}}}

    async def responses(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._responses_output(payload)

    async def responses_stream(self, payload: dict[str, Any]):
        resp = self._responses_output(payload)
        yield {"type": "response.created", "response": {"id": "resp_1"}}
        for item in resp["output"]:
            if item["type"] == "reasoning":
                for part in item["summary"]:
                    yield {"type": "response.reasoning_summary_part.added"}
                    for piece in re.findall(r".{1,4}", part["text"], re.DOTALL):
                        yield {"type": "response.reasoning_summary_text.delta", "delta": piece}
            elif item["type"] == "message":
                for piece in re.findall(r".{1,4}", item["content"][0]["text"], re.DOTALL):
                    yield {"type": "response.output_text.delta", "delta": piece}
            elif item["type"] == "function_call":
                yield {"type": "response.output_item.added", "item": {**item, "arguments": ""}}
                yield {"type": "response.function_call_arguments.delta", "delta": item["arguments"]}
            yield {"type": "response.output_item.done", "item": item}
        yield {"type": "response.completed", "response": resp}

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        # Bag-of-words-Hashing: ähnliche Wörter → ähnliche Vektoren
        out = []
        for t in texts:
            v = np.zeros(64, dtype=np.float32)
            for w in re.findall(r"\w{3,}", t.lower()):
                v[int(hashlib.md5(w[:5].encode()).hexdigest(), 16) % 64] += 1
            out.append(v.tolist())
        return out

    async def model_info(self) -> list[dict[str, Any]]:
        return self.model_info_rows

    async def close(self) -> None:
        pass


def tool_call(name: str, args: dict[str, Any], call_id: str = "call_1") -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        db_path=str(tmp_path / "brain.db"),
        chat_models="gpt-5",
        user_name="Alex",
        enable_worker=False,
        export_dir=str(tmp_path / "vault"),
        embedding_model="",
        mcp_config=str(tmp_path / "mcp.json"),
        mcp_secrets=str(tmp_path / "secrets.env"),
        mcp_log_dir=str(tmp_path / "mcp-logs"),
        mcp_watch_interval=0,
        web_dir=str(tmp_path / "web"),
        files_dir=str(tmp_path / "files"),
    )


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
async def services(settings, fake_llm):
    svc = Services(settings, llm=fake_llm)
    await svc.startup()
    yield svc
    await svc.shutdown()


@pytest.fixture
async def client(services):
    app = create_app(services=services)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


def parse_sse(text: str) -> list[dict[str, Any]]:
    out = []
    for line in text.splitlines():
        if line.startswith("data: ") and line[6:] != "[DONE]":
            out.append(json.loads(line[6:]))
    return out


def sse_content(text: str) -> str:
    return "".join(
        (c["choices"][0]["delta"].get("content") or "") for c in parse_sse(text) if c.get("choices")
    )

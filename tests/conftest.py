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
        self.handler: Callable[[dict[str, Any]], dict[str, Any]] = lambda p: {"content": "OK"}
        self.background: Callable[[dict[str, Any]], dict[str, Any]] = lambda p: {
            "content": json.dumps({"entities": [], "facts": [], "relations": []})
        }

    def _respond(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(payload)
        system = payload["messages"][0]["content"] if payload["messages"] else ""
        if "Gedächtnis-Modul" in system or "Kernprofil" in system or "zusammen, damit" in system:
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

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        # Bag-of-words-Hashing: ähnliche Wörter → ähnliche Vektoren
        out = []
        for t in texts:
            v = np.zeros(64, dtype=np.float32)
            for w in re.findall(r"\w{3,}", t.lower()):
                v[int(hashlib.md5(w[:5].encode()).hexdigest(), 16) % 64] += 1
            out.append(v.tolist())
        return out

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

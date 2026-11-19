"""OpenAI-kompatibler Adapter (für Open WebUI): Request → ChatSession → Chat-Completion-Chunks."""

from __future__ import annotations

import json
import time
import uuid
from typing import TYPE_CHECKING, Any, AsyncIterator

from ..llm import UpstreamError
from ..openwebui import activity_block, conversation_id, strip_activity
from ..tools.permissions import parse_mode_command
from .session import ChatSession

if TYPE_CHECKING:
    from ..services import Services

MODES = ("brain", "incognito")


def parse_model(model_id: str, default_model: str) -> tuple[str, str]:
    """'brain/gpt-5' → ('brain', 'gpt-5'); unbekanntes Präfix → Modus brain."""
    if "/" in model_id:
        mode, _, upstream = model_id.partition("/")
        if mode in MODES and upstream:
            return mode, upstream
    if model_id in MODES:
        return model_id, default_model
    return "brain", model_id


def _strip_mode_commands(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str | None]:
    """Entfernt /plan, /ask, /auto am Anfang von Nutzernachrichten; liefert den Befehl der letzten."""
    out, last_cmd = [], None
    last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    for i, m in enumerate(messages):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            cmd, rest = parse_mode_command(m["content"])
            if cmd:
                m = {**m, "content": rest.strip() or f"[Modus auf „{cmd}“ gestellt]"}
                if i == last_user:
                    last_cmd = cmd
        out.append(m)
    return out, last_cmd


class OpenAIChat:
    def __init__(self, services: "Services", body: dict[str, Any], headers: dict[str, str]) -> None:
        self.s = services
        settings = services.settings
        self.requested_model = body.get("model") or ""
        default_model = settings.chat_model_list[0] if settings.chat_model_list else "gpt-5"
        mode, upstream = parse_model(self.requested_model, default_model)
        incognito = mode == "incognito"

        incoming = strip_activity(list(body.get("messages") or []))
        conv_id = conversation_id(headers, incoming)
        incoming, mode_cmd = _strip_mode_commands(incoming)
        if mode_cmd:
            if incognito:
                services.ephemeral_modes[conv_id] = mode_cmd
            else:
                services.store.set_permission_mode(conv_id, mode_cmd, mode)
        stored = services.ephemeral_modes.get(conv_id) if incognito else services.store.get_permission_mode(conv_id)

        params = dict(body)
        client_tools = body.get("tools") or []
        if not client_tools:
            params.pop("tool_choice", None)
        self.session = ChatSession(
            services,
            messages=incoming,
            model=upstream,
            conversation_id=conv_id,
            incognito=incognito,
            permission_mode=mode_cmd or stored or settings.permission_mode,
            params=params,
            client_tools=client_tools,
            conv_mode=mode,
            mode_changed=mode_cmd,
        )
        if client_tools and body.get("tool_choice"):
            self.session.params["tool_choice"] = body["tool_choice"]
        self.completion_id = f"chatcmpl-{uuid.uuid4().hex}"
        self.created = int(time.time())

    async def prepare(self) -> None:
        await self.session.prepare()

    def _chunk(self, delta: dict[str, Any], finish_reason: str | None = None, usage: Any = None) -> str:
        data: dict[str, Any] = {
            "id": self.completion_id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.requested_model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        if usage:
            data["usage"] = usage
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    async def stream(self) -> AsyncIterator[str]:
        show = self.s.settings.show_tool_activity
        role_sent = False

        def with_role(delta: dict[str, Any]) -> dict[str, Any]:
            nonlocal role_sent
            if not role_sent:
                role_sent = True
                return {"role": "assistant", **delta}
            return delta

        async for ev in self.session.run():
            t = ev["type"]
            if t == "text":
                yield self._chunk(with_role({"content": ev["delta"]}))
            elif t == "reasoning":
                yield self._chunk(with_role({"reasoning_content": ev["delta"]}))
            elif t in ("tools_done", "activity") and show:
                lines = ev.get("descriptions") or ev.get("lines") or []
                if lines:
                    yield self._chunk(with_role({"content": activity_block(lines)}))
            elif t == "client_tool_calls":
                calls = [{"index": i, **c} for i, c in enumerate(ev["calls"])]
                yield self._chunk(with_role({"tool_calls": calls}))
            elif t == "done":
                if not role_sent:
                    yield self._chunk(with_role({"content": ""}))
                yield self._chunk({}, ev["finish_reason"], ev.get("usage"))
            elif t == "error":
                yield self._chunk(with_role({"content": f"\n\n⚠️ Fehler von der LLM-API: {ev['message'][:500]}"}), "stop")
        if self.session.completed:
            self.session.persist()
        yield "data: [DONE]\n\n"

    async def complete(self) -> dict[str, Any]:
        show = self.s.settings.show_tool_activity
        text, reasoning, finish, usage, client_calls = [], [], "stop", None, []
        async for ev in self.session.run():
            t = ev["type"]
            if t == "text":
                text.append(ev["delta"])
            elif t == "reasoning":
                reasoning.append(ev["delta"])
            elif t in ("tools_done", "activity") and show:
                lines = ev.get("descriptions") or ev.get("lines") or []
                if lines:
                    text.append(activity_block(lines))
            elif t == "client_tool_calls":
                client_calls = ev["calls"]
            elif t == "done":
                finish, usage = ev["finish_reason"], ev.get("usage")
            elif t == "error":
                raise UpstreamError(502, ev["message"])
        message: dict[str, Any] = {"role": "assistant", "content": "".join(text)}
        if reasoning:
            message["reasoning_content"] = "".join(reasoning)
        if client_calls:
            message["tool_calls"] = client_calls
        if self.session.completed:
            self.session.persist()
        resp: dict[str, Any] = {
            "id": self.completion_id,
            "object": "chat.completion",
            "created": self.created,
            "model": self.requested_model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        }
        if usage:
            resp["usage"] = usage
        return resp

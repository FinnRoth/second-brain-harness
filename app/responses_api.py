"""Übersetzung Chat Completions ⇄ Responses API.

Nach vorne (Open WebUI, Agent-Loop) bleibt alles im Chat-Completions-Format.
Nur der Upstream-Call geht an LiteLLMs /v1/responses. Das ist nötig für Modelle,
die Tools + Reasoning nur über die Responses API unterstützen.

- stateless (`store: false`): nichts wird beim Anbieter gespeichert
- Reasoning-Items (verschlüsselt) werden innerhalb des Tool-Loops zurückgereicht,
  damit das Modell zwischen Tool-Aufrufen seinen Gedankengang behält
- Reasoning-Zusammenfassungen werden als `reasoning_content` gestreamt
  (Open WebUI zeigt sie als einklappbaren „Denkprozess“)
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import TYPE_CHECKING, Any, AsyncIterator

from .llm import UpstreamError

if TYPE_CHECKING:
    from .config import Settings
    from .llm import LLM

log = logging.getLogger(__name__)

# Privates Feld an Assistenten-Nachrichten / Chunks: Original-Output-Items der Responses API
RAW_KEY = "_responses_output"


# ------------------------------------------------------------------ Request
def _content_parts(content: Any) -> Any:
    if not isinstance(content, list):
        return content if content is not None else ""
    parts = []
    for p in content:
        if not isinstance(p, dict):
            continue
        t = p.get("type")
        if t == "text":
            parts.append({"type": "input_text", "text": p.get("text") or ""})
        elif t == "image_url":
            url = p.get("image_url")
            url = url.get("url") if isinstance(url, dict) else url
            parts.append({"type": "input_image", "image_url": url})
        elif t in ("input_text", "input_image", "input_file"):
            parts.append(p)
    return parts


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("text"))
    return ""


def messages_to_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role in ("system", "developer"):
            items.append({"role": "system", "content": _text(m.get("content"))})
        elif role == "user":
            items.append({"role": "user", "content": _content_parts(m.get("content"))})
        elif role == "assistant":
            raw = m.get(RAW_KEY)
            if raw:
                items.extend(raw)
                continue
            text = _text(m.get("content"))
            if text:
                items.append({"role": "assistant", "content": text})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                items.append(
                    {
                        "type": "function_call",
                        "call_id": tc.get("id"),
                        "name": fn.get("name"),
                        "arguments": fn.get("arguments") or "{}",
                    }
                )
        elif role == "tool":
            items.append(
                {"type": "function_call_output", "call_id": m.get("tool_call_id"), "output": _text(m.get("content"))}
            )
    return items


def _tool(t: dict[str, Any]) -> dict[str, Any]:
    if t.get("type") == "function" and "function" in t:
        fn = t["function"]
        out = {"type": "function", "name": fn["name"], "parameters": fn.get("parameters") or {"type": "object"}}
        if fn.get("description"):
            out["description"] = fn["description"]
        if "strict" in fn:
            out["strict"] = fn["strict"]
        return out
    return t


def _tool_choice(tc: Any) -> Any:
    if isinstance(tc, dict) and tc.get("type") == "function" and "function" in tc:
        return {"type": "function", "name": tc["function"].get("name")}
    return tc


def to_responses_request(
    payload: dict[str, Any], reasoning_summary: str = "", keep_reasoning: bool = True
) -> dict[str, Any]:
    req: dict[str, Any] = {
        "model": payload["model"],
        "input": messages_to_input(payload.get("messages") or []),
        "store": False,
    }
    if payload.get("tools"):
        req["tools"] = [_tool(t) for t in payload["tools"]]
        if payload.get("tool_choice") is not None:
            req["tool_choice"] = _tool_choice(payload["tool_choice"])
        if payload.get("parallel_tool_calls") is not None:
            req["parallel_tool_calls"] = payload["parallel_tool_calls"]
    for key in ("temperature", "top_p", "user"):
        if payload.get(key) is not None:
            req[key] = payload[key]
    max_tokens = payload.get("max_completion_tokens") or payload.get("max_tokens")
    if max_tokens:
        req["max_output_tokens"] = max_tokens

    effort = payload.get("reasoning_effort")
    if effort:
        reasoning: dict[str, Any] = {"effort": effort}
        if reasoning_summary and effort != "none":
            reasoning["summary"] = reasoning_summary
        req["reasoning"] = reasoning
        if keep_reasoning and effort != "none":
            req["include"] = ["reasoning.encrypted_content"]

    text: dict[str, Any] = {}
    rf = payload.get("response_format")
    if isinstance(rf, dict):
        if rf.get("type") == "json_object":
            text["format"] = {"type": "json_object"}
        elif rf.get("type") == "json_schema" and isinstance(rf.get("json_schema"), dict):
            js = rf["json_schema"]
            text["format"] = {
                "type": "json_schema",
                "name": js.get("name", "schema"),
                "schema": js.get("schema", {}),
                "strict": js.get("strict", False),
            }
    if payload.get("verbosity"):
        text["verbosity"] = payload["verbosity"]
    if text:
        req["text"] = text
    return req


# ----------------------------------------------------------------- Response
def _usage(u: dict[str, Any] | None) -> dict[str, Any] | None:
    if not u:
        return None
    out = {
        "prompt_tokens": u.get("input_tokens", 0),
        "completion_tokens": u.get("output_tokens", 0),
        "total_tokens": u.get("total_tokens", u.get("input_tokens", 0) + u.get("output_tokens", 0)),
    }
    details = u.get("output_tokens_details") or {}
    if details.get("reasoning_tokens") is not None:
        out["completion_tokens_details"] = {"reasoning_tokens": details["reasoning_tokens"]}
    return out


def _raw_items(output: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """Output-Items zum Zurückreichen – nur, wenn Reasoning verschlüsselt vorliegt.

    Ohne encrypted_content (store=false) würden Reasoning-Items beim nächsten Call
    nicht gefunden; dann rekonstruieren wir die Function-Calls lieber ohne IDs.
    """
    reasoning = [i for i in output if i.get("type") == "reasoning"]
    if not reasoning or any(not i.get("encrypted_content") for i in reasoning):
        return None
    return [i for i in output if i.get("type") in ("reasoning", "function_call", "message")]


def _reasoning_summary(item: dict[str, Any]) -> str:
    return "\n\n".join(s.get("text", "") for s in item.get("summary") or [] if s.get("text"))


def _finish_reason(resp: dict[str, Any], has_calls: bool) -> str:
    if has_calls:
        return "tool_calls"
    if resp.get("status") == "incomplete":
        reason = (resp.get("incomplete_details") or {}).get("reason")
        return "length" if reason == "max_output_tokens" else "content_filter"
    return "stop"


def from_response(resp: dict[str, Any]) -> dict[str, Any]:
    output = resp.get("output") or []
    text_parts, reasoning_parts, tool_calls = [], [], []
    for item in output:
        t = item.get("type")
        if t == "message":
            for c in item.get("content") or []:
                if c.get("type") in ("output_text", "text"):
                    text_parts.append(c.get("text") or "")
                elif c.get("type") == "refusal":
                    text_parts.append(c.get("refusal") or "")
        elif t == "reasoning":
            s = _reasoning_summary(item)
            if s:
                reasoning_parts.append(s)
        elif t == "function_call":
            tool_calls.append(
                {
                    "id": item.get("call_id") or item.get("id"),
                    "type": "function",
                    "function": {"name": item.get("name"), "arguments": item.get("arguments") or "{}"},
                }
            )
    message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
    if reasoning_parts:
        message["reasoning_content"] = "\n\n".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = tool_calls
    out = {
        "id": resp.get("id") or f"resp-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(resp.get("created_at") or time.time()),
        "model": resp.get("model"),
        "choices": [{"index": 0, "message": message, "finish_reason": _finish_reason(resp, bool(tool_calls))}],
    }
    usage = _usage(resp.get("usage"))
    if usage:
        out["usage"] = usage
    raw = _raw_items(output)
    if raw:
        out[RAW_KEY] = raw
    return out


def _chunk(delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
    return {"object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}


async def events_to_chunks(events: AsyncIterator[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    """Responses-Stream-Events → Chat-Completion-Chunks (inkl. reasoning_content)."""
    call_index = 0
    summary_parts = 0
    async for ev in events:
        t = ev.get("type", "")
        if t == "response.output_text.delta":
            if ev.get("delta"):
                yield _chunk({"content": ev["delta"]})
        elif t == "response.refusal.delta":
            if ev.get("delta"):
                yield _chunk({"content": ev["delta"]})
        elif t == "response.reasoning_summary_part.added":
            if summary_parts:
                yield _chunk({"reasoning_content": "\n\n"})
            summary_parts += 1
        elif t in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
            if ev.get("delta"):
                yield _chunk({"reasoning_content": ev["delta"]})
        elif t == "response.output_item.done":
            item = ev.get("item") or {}
            if item.get("type") == "function_call":
                yield _chunk(
                    {
                        "tool_calls": [
                            {
                                "index": call_index,
                                "id": item.get("call_id") or item.get("id"),
                                "type": "function",
                                "function": {"name": item.get("name"), "arguments": item.get("arguments") or "{}"},
                            }
                        ]
                    }
                )
                call_index += 1
        elif t in ("response.completed", "response.incomplete"):
            resp = ev.get("response") or {}
            final = _chunk({}, _finish_reason(resp, call_index > 0))
            usage = _usage(resp.get("usage"))
            if usage:
                final["usage"] = usage
            raw = _raw_items(resp.get("output") or [])
            if raw:
                final[RAW_KEY] = raw
            yield final
            return
        elif t in ("response.failed", "error"):
            err = (ev.get("response") or {}).get("error") or ev.get("error") or ev
            raise UpstreamError(502, err.get("message") if isinstance(err, dict) else str(err))


# ------------------------------------------------------------------ Backend
class ResponsesBackend:
    """Schickt Chat-Payloads über /v1/responses und liefert Chat-Format zurück.

    Lehnt das Upstream optionale Features ab (Reasoning-Zusammenfassung,
    verschlüsseltes Reasoning), werden sie einmalig abgeschaltet und die Anfrage
    wiederholt.
    """

    def __init__(self, llm: "LLM", settings: "Settings") -> None:
        self.llm = llm
        self.summary = settings.responses_reasoning_summary.strip()
        self.keep_reasoning = True

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        return to_responses_request(payload, self.summary, self.keep_reasoning)

    def _downgrade(self, e: UpstreamError) -> bool:
        msg = e.message.lower()
        if self.summary and "summary" in msg:
            log.warning("Upstream lehnt Reasoning-Zusammenfassungen ab – deaktiviert (%s)", e.message[:200])
            self.summary = ""
            return True
        if self.keep_reasoning and ("encrypted_content" in msg or "include" in msg):
            log.warning("Upstream lehnt verschlüsseltes Reasoning ab – deaktiviert (%s)", e.message[:200])
            self.keep_reasoning = False
            return True
        return False

    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        while True:
            try:
                return from_response(await self.llm.responses(self._request(payload)))
            except UpstreamError as e:
                if not self._downgrade(e):
                    raise

    async def chat_stream(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        while True:
            started = False
            try:
                async for chunk in events_to_chunks(self.llm.responses_stream(self._request(payload))):
                    started = True
                    yield chunk
                return
            except UpstreamError as e:
                if started or not self._downgrade(e):
                    raise


def strip_private(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Entfernt Responses-Rohdaten, bevor Nachrichten an /chat/completions gehen."""
    return [{k: v for k, v in m.items() if k != RAW_KEY} if RAW_KEY in m else m for m in messages]

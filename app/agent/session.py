"""Eine Chat-Anfrage: Kontext anreichern → Agent-Loop mit Tools → speichern.

Ablauf:
1. Modus-Befehle (/plan, /ask, /auto) auswerten, offene Freigaben erledigen
2. Systemprompt (stabil) + <kontext>-Block (pro Nachricht) bauen
3. Agent-Loop: Upstream-Call (Chat Completions oder Responses API), Tools ausführen
   (Gedächtnis, MCP), bis das Modell antwortet
4. Turn speichern, Dreaming einplanen
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator

from ..llm import UpstreamError
from ..memory.retrieval import text_of
from ..openwebui import activity_block, conversation_id, strip_activity
from ..responses_api import RAW_KEY, strip_private
from ..tools.base import ToolContext, ToolResult
from ..tools.memory import describe_call
from ..tools.permissions import MODE_DESCRIPTIONS, classify_reply, parse_mode_command
from ..tools.registry import TOOL_SEARCH, ToolRegistry, ToolSet, short_args

if TYPE_CHECKING:
    from ..services import Services

log = logging.getLogger(__name__)

MODES = ("brain", "incognito")
# Parameter, die 1:1 an LiteLLM durchgereicht werden. Alles andere, was Open WebUI
# evtl. mitschickt (metadata, chat_id, features …), wird verworfen.
PASSTHROUGH_PARAMS = {
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "stop",
    "presence_penalty",
    "frequency_penalty",
    "seed",
    "logit_bias",
    "user",
    "reasoning_effort",
    "verbosity",
    "response_format",
    "parallel_tool_calls",
}


def parse_model(model_id: str, default_model: str) -> tuple[str, str]:
    """'brain/gpt-5' → ('brain', 'gpt-5'); unbekanntes Präfix → Modus brain."""
    if "/" in model_id:
        mode, _, upstream = model_id.partition("/")
        if mode in MODES and upstream:
            return mode, upstream
    if model_id in MODES:
        return model_id, default_model
    return "brain", model_id


@dataclass
class _ToolCall:
    id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass
class _ToolCallAccumulator:
    calls: dict[int, _ToolCall] = field(default_factory=dict)

    def add(self, deltas: list[dict[str, Any]]) -> None:
        for i, d in enumerate(deltas):
            idx = d.get("index", i)
            call = self.calls.setdefault(idx, _ToolCall())
            if d.get("id"):
                call.id = d["id"]
            fn = d.get("function") or {}
            if fn.get("name"):
                call.name += fn["name"]
            if fn.get("arguments"):
                call.arguments += fn["arguments"]

    def result(self) -> list[_ToolCall]:
        out = []
        for idx in sorted(self.calls):
            c = self.calls[idx]
            if not c.id:
                c.id = f"call_{uuid.uuid4().hex[:12]}"
            out.append(c)
        return out


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


class ChatSession:
    def __init__(self, services: "Services", body: dict[str, Any], headers: dict[str, str]) -> None:
        self.s = services
        self.settings = services.settings
        self.body = body
        self.requested_model = body.get("model") or ""
        default_model = self.settings.chat_model_list[0] if self.settings.chat_model_list else "gpt-5"
        self.mode, self.upstream_model = parse_model(self.requested_model, default_model)
        self.incognito = self.mode == "incognito"
        self.client_tools: list[dict[str, Any]] = body.get("tools") or []
        self.params = {k: v for k, v in body.items() if k in PASSTHROUGH_PARAMS and v is not None}
        if self.client_tools and body.get("tool_choice"):
            self.params["tool_choice"] = body["tool_choice"]

        incoming = strip_activity(list(body.get("messages") or []))
        self.conversation_id = conversation_id(headers, incoming)
        self.incoming, mode_cmd = _strip_mode_commands(incoming)
        self.turn_index = sum(1 for m in self.incoming if m.get("role") == "user")
        self.mode_changed = mode_cmd
        permission_mode = mode_cmd or self._stored_permission_mode() or self.settings.permission_mode
        if mode_cmd:
            self._store_permission_mode(mode_cmd)

        self.registry: ToolRegistry = services.tools
        self.ctx = ToolContext(
            conversation_id=self.conversation_id, incognito=self.incognito, permission_mode=permission_mode
        )
        self.toolset: ToolSet = ToolSet({}, {})
        self.tool_log: list[dict[str, Any]] = []
        self.pre_activity: list[str] = []
        self.messages: list[dict[str, Any]] = []
        self.tools_disabled = False
        self._conflict_handled = False
        # Upstream-API: fest konfiguriert oder zur Laufzeit gelernt (dann mit Fallback)
        self._responses_forced = (
            self.settings.upstream_api == "responses" or self.upstream_model in self.settings.responses_model_list
        )
        self.use_responses = self._responses_forced or (
            self.upstream_model in services.responses_models
            and self.upstream_model not in services.responses_unsupported
        )
        self.completion_id = f"chatcmpl-{uuid.uuid4().hex}"
        self.created = int(time.time())

    # ------------------------------------------------------------ Freigabe-Modus
    def _stored_permission_mode(self) -> str | None:
        if self.incognito:
            return self.s.ephemeral_modes.get(self.conversation_id)
        return self.s.store.get_permission_mode(self.conversation_id)

    def _store_permission_mode(self, mode: str) -> None:
        if self.incognito:
            self.s.ephemeral_modes[self.conversation_id] = mode
        else:
            self.s.store.set_permission_mode(self.conversation_id, mode, self.mode)

    # ---------------------------------------------------------------- Vorbereitung
    async def prepare(self) -> None:
        self._refresh_tools()
        approvals_note = await self._handle_pending_approvals()

        mode_hints = []
        if self.incognito:
            mode_hints.append(
                "Dies ist ein **Inkognito-Chat**: Nichts aus diesem Gespräch wird gespeichert. "
                "Du kannst das Gedächtnis lesen, aber nicht verändern."
            )
        pb = self.s.prompt_builder
        system, saved_ids = pb.system_prompt(self.conversation_id, mode_hints, self.s.mcp_instructions())
        turn = await pb.turn_context(self.incoming, exclude_fact_ids=saved_ids)

        extra = []
        has_external = any(not r.spec.source == "gedächtnis" for r in self.toolset.tools.values()) or bool(
            self.toolset.deferred
        )
        if has_external or self.ctx.permission_mode != self.settings.permission_mode or self.mode_changed:
            extra.append(f"Freigabe-Modus: {self.ctx.permission_mode}. {MODE_DESCRIPTIONS[self.ctx.permission_mode]}")
        if self.mode_changed:
            extra.append(f"Der Nutzer hat gerade in den Modus „{self.mode_changed}“ gewechselt. Bestätige das kurz.")
        if approvals_note:
            extra.append(approvals_note)
        context = "\n\n".join([turn.text, *extra])
        self.messages = [{"role": "system", "content": system}, *pb.inject(self.incoming, context)]

        if self.turn_index == 1 and not self.incognito:
            # Neuer Chat: ausstehendes Dreaming anderer Chats vorziehen, damit es bald im Gedächtnis ist
            if self.s.store.expedite_jobs("dream_conversation", exclude_dedupe_key=f"dream:{self.conversation_id}"):
                self.s.worker.notify()
        if (
            not self.use_responses
            and self.upstream_model in self.s.tool_reasoning_conflicts
            and "reasoning_effort" in self.params
        ):
            self._resolve_conflict(allow_responses=False)

    def _refresh_tools(self) -> None:
        self.toolset = self.registry.resolve(self.ctx)

    async def _handle_pending_approvals(self) -> str:
        """Offene Freigaben: kurze Zustimmung führt aus, Ablehnung verwirft, alles andere lässt verfallen."""
        store = self.s.store
        pending = store.pending_approvals(self.conversation_id)
        if not pending:
            return ""
        users = [m for m in self.incoming if m.get("role") == "user"]
        verdict = classify_reply(text_of(users[-1].get("content")) if users else "")
        if verdict == "yes":
            lines = []
            for a in pending:
                result = await self._execute_approved(a["tool_name"], a["arguments"])
                store.resolve_approval(a["id"], "approved", result.text[:4000])
                self.tool_log.append(
                    {"name": a["tool_name"], "arguments": a["arguments"], "result": result.text[:500], "approved": True}
                )
                self.pre_activity.append(f"✅ Freigegeben: {self._describe(a['tool_name'], a['arguments'])}")
                lines.append(f"- {a['tool_name']} {short_args(a['arguments'])} → {result.text[:3000]}")
            return (
                "Der Nutzer hat die vorgemerkten Aktionen freigegeben; sie wurden soeben ausgeführt:\n"
                + "\n".join(lines)
                + "\nBerichte kurz über das Ergebnis."
            )
        status = "rejected" if verdict == "no" else "expired"
        for a in pending:
            store.resolve_approval(a["id"], status)
        if verdict == "no":
            names = ", ".join(a["tool_name"] for a in pending)
            return f"Der Nutzer hat die vorgemerkten Aktionen abgelehnt ({names}). Sie wurden NICHT ausgeführt."
        return ""

    async def _execute_approved(self, name: str, args: dict[str, Any]) -> ToolResult:
        rt = self.toolset.tools.get(name) or self.toolset.deferred.get(name)
        if rt is None or rt.provider is None:
            return ToolResult(f"Fehler: Tool '{name}' ist im aktuellen Modus nicht verfügbar.", is_error=True)
        log.info("Freigegebener Tool-Aufruf %s(%s)", name, short_args(args, 200))
        return await rt.provider.call(name, args, self.ctx)

    # ------------------------------------------- Tools vs. reasoning_effort
    @staticmethod
    def _is_tool_reasoning_conflict(e: UpstreamError) -> bool:
        msg = e.message.lower()
        return "reasoning_effort" in msg and ("tool" in msg or "function" in msg)

    def _resolve_conflict(self, allow_responses: bool = True) -> None:
        self._conflict_handled = True
        strategy = self.settings.reasoning_tools_conflict
        if strategy == "use_responses" and allow_responses and self.upstream_model not in self.s.responses_unsupported:
            self.use_responses = True
            self.s.responses_models.add(self.upstream_model)
            log.info("%s: wechsle auf /v1/responses (Tools + Reasoning)", self.upstream_model)
            return
        self.s.tool_reasoning_conflicts.add(self.upstream_model)
        if strategy == "prefer_tools":
            self.params["reasoning_effort"] = "none"
            log.info("%s: reasoning_effort='none', damit Tools nutzbar sind", self.upstream_model)
            return
        self.tools_disabled = True
        self.messages[0] = {
            **self.messages[0],
            "content": self.messages[0]["content"]
            + "\n\n_Hinweis: In diesem Chat stehen dir keine Tools zur Verfügung (auch keine Gedächtnis-Tools). "
            "Nutze die bereitgestellten Erinnerungen; neue Informationen werden automatisch gespeichert._",
        }
        log.info("%s: Tools deaktiviert (Modell erlaubt keine Tools mit reasoning_effort)", self.upstream_model)

    def _try_recover(self, e: UpstreamError) -> bool:
        """True, wenn die Anfrage nach einer Anpassung wiederholt werden kann."""
        if self.use_responses and not self._responses_forced and e.status not in (0, 429):
            log.warning(
                "%s: /v1/responses fehlgeschlagen (%s) – Fallback auf /v1/chat/completions ohne Tools",
                self.upstream_model,
                e.message[:300],
            )
            self.use_responses = False
            self.s.responses_models.discard(self.upstream_model)
            self.s.responses_unsupported.add(self.upstream_model)
            if "reasoning_effort" in self.params:
                self._resolve_conflict(allow_responses=False)
            return True
        if self._conflict_handled or self.use_responses or not self._is_tool_reasoning_conflict(e):
            return False
        self._resolve_conflict()
        return True

    # ----------------------------------------------------------------- Upstream
    def _upstream(self):
        return self.s.responses if self.use_responses else self.s.llm

    async def _upstream_stream(self, allow_internal: bool) -> AsyncIterator[dict[str, Any]]:
        while True:
            started = False
            try:
                async for chunk in self._upstream().chat_stream(self._payload(allow_internal)):
                    started = True
                    yield chunk
                return
            except UpstreamError as e:
                if started or not self._try_recover(e):
                    raise

    async def _upstream_chat(self, allow_internal: bool) -> dict[str, Any]:
        while True:
            try:
                return await self._upstream().chat(self._payload(allow_internal))
            except UpstreamError as e:
                if not self._try_recover(e):
                    raise

    def _tools(self, allow_internal: bool) -> list[dict[str, Any]]:
        if self.tools_disabled:
            return []
        tools = list(self.client_tools)
        if allow_internal:
            tools = self.registry.definitions(self.toolset.tools) + tools
        return tools

    def _payload(self, allow_internal_tools: bool) -> dict[str, Any]:
        messages = self.messages if self.use_responses else strip_private(self.messages)
        payload = {**self.params, "model": self.upstream_model, "messages": messages}
        tools = self._tools(allow_internal_tools)
        if tools:
            payload["tools"] = tools
        else:
            payload.pop("tool_choice", None)
            payload.pop("parallel_tool_calls", None)
        return payload

    @property
    def internal_names(self) -> set[str]:
        return set(self.toolset.tools)

    # ------------------------------------------------------------------- Tools
    def _describe(self, name: str, args: dict[str, Any]) -> str:
        rt = self.toolset.tools.get(name) or self.toolset.deferred.get(name)
        if rt and rt.spec.source.startswith("mcp:"):
            return f"🔧 {rt.spec.source[4:]} · {rt.spec.title} {short_args(args, 80)}"
        if name == TOOL_SEARCH:
            return f"🔎 Tools gesucht: „{args.get('query', '')}“"
        return describe_call(name, args)

    async def _run_tools(
        self, calls: list[_ToolCall], content: str, raw: list[dict[str, Any]] | None = None
    ) -> list[str]:
        assistant: dict[str, Any] = {
            "role": "assistant",
            "content": content or None,
            "tool_calls": [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments or "{}"}}
                for c in calls
            ],
        }
        if raw:
            assistant[RAW_KEY] = raw  # Responses API: Reasoning-Items für den nächsten Schritt
        self.messages.append(assistant)
        descriptions = []
        for c in calls:
            try:
                args = json.loads(c.arguments or "{}")
                if not isinstance(args, dict):
                    raise ValueError("kein Objekt")
            except (json.JSONDecodeError, ValueError):
                args = None
            if args is None:
                result = ToolResult("Fehler: Argumente sind kein gültiges JSON-Objekt.", is_error=True)
                desc = f"⚠️ {c.name}: ungültige Argumente"
                args = {}
            else:
                result, desc = await self._execute(c.name, args)
            self.messages.append({"role": "tool", "tool_call_id": c.id, "content": result.text})
            self.tool_log.append({"name": c.name, "arguments": args, "result": result.text[:500]})
            descriptions.append(desc)
        self._refresh_tools()  # tool_search kann neue Tools freigeschaltet haben
        return descriptions

    async def _execute(self, name: str, args: dict[str, Any]) -> tuple[ToolResult, str]:
        log.info("Tool-Aufruf %s(%s)", name, short_args(args, 200))
        if name == TOOL_SEARCH:
            return self.registry.search(self.toolset, str(args.get("query", "")), self.ctx), self._describe(name, args)
        rt = self.toolset.tools.get(name)
        if rt is None or rt.provider is None:
            return ToolResult(f"Fehler: Tool '{name}' ist nicht verfügbar.", is_error=True), f"⚠️ {name} nicht verfügbar"
        if rt.decision == "ask":
            desc = self._describe(name, args)
            aid = self.s.store.create_approval(self.conversation_id, name, args, desc)
            user = self.settings.user_name
            return (
                ToolResult(
                    f"Diese Aktion braucht die Freigabe von {user} und wurde vorgemerkt (Freigabe #{aid}), "
                    "aber NICHT ausgeführt. Erkläre kurz, was du tun möchtest und warum, und bitte um Bestätigung: "
                    f"{user} antwortet mit „ok“ zum Ausführen oder „nein“ zum Abbrechen. Rufe das Tool nicht erneut auf."
                ),
                f"⏸️ Wartet auf Freigabe: {desc}",
            )
        return await rt.provider.call(name, args, self.ctx), self._describe(name, args)

    def _chunk(self, delta: dict[str, Any], finish_reason: str | None = None) -> str:
        data = {
            "id": self.completion_id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.requested_model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

    # ------------------------------------------------------------------ Streaming
    async def stream(self) -> AsyncIterator[str]:
        final_parts: list[str] = []
        role_sent = False
        reasoning_sent = False
        completed = False
        usage: dict[str, Any] | None = None
        try:
            if self.pre_activity and self.settings.show_tool_activity:
                yield self._chunk({"role": "assistant", "content": activity_block(self.pre_activity)})
                role_sent = True
            max_iter = self.settings.max_tool_iterations
            for iteration in range(max_iter + 1):
                allow_internal = iteration < max_iter
                acc = _ToolCallAccumulator()
                finish_reason = None
                iteration_text: list[str] = []
                iteration_reasoning = False
                raw = None
                async for chunk in self._upstream_stream(allow_internal):
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    if chunk.get(RAW_KEY):
                        raw = chunk[RAW_KEY]
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta") or {}
                    if delta.get("tool_calls"):
                        acc.add(delta["tool_calls"])
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                    out = {k: v for k, v in delta.items() if k not in ("tool_calls", "role") and v is not None}
                    has_payload = any(v for v in out.values())
                    if not role_sent and (has_payload or delta.get("role")):
                        out["role"] = "assistant"
                        role_sent = True
                        has_payload = True
                    if has_payload:
                        if out.get("content"):
                            iteration_text.append(out["content"])
                        if out.get("reasoning_content"):
                            if reasoning_sent and not iteration_reasoning:
                                out["reasoning_content"] = "\n\n" + out["reasoning_content"]
                            reasoning_sent = iteration_reasoning = True
                        yield self._chunk(out)

                calls = acc.result()
                text = "".join(iteration_text)
                final_parts.append(text)
                internal = self.internal_names
                if calls and allow_internal and all(c.name in internal for c in calls):
                    descriptions = await self._run_tools(calls, text, raw)
                    if self.settings.show_tool_activity:
                        yield self._chunk({"content": activity_block(descriptions)})
                    continue
                if calls:
                    # Tools des Clients (Open WebUI native function calling) → an den Client durchreichen
                    client_calls = [c for c in calls if c.name not in internal]
                    yield self._chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": i,
                                    "id": c.id,
                                    "type": "function",
                                    "function": {"name": c.name, "arguments": c.arguments},
                                }
                                for i, c in enumerate(client_calls)
                            ]
                        }
                    )
                    finish_reason = "tool_calls"
                final = self._chunk({}, finish_reason or "stop")
                if usage:
                    data = json.loads(final[6:])
                    data["usage"] = usage
                    final = f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                yield final
                completed = True
                break
        except UpstreamError as e:
            log.error("Upstream-Fehler: %s", e)
            if not role_sent:
                yield self._chunk({"role": "assistant", "content": ""})
            yield self._chunk({"content": f"\n\n⚠️ Fehler von der LLM-API: {e.message[:500]}"}, "stop")
        if completed:
            self.persist("".join(final_parts))
        yield "data: [DONE]\n\n"

    # -------------------------------------------------------------- Ohne Streaming
    async def complete(self) -> dict[str, Any]:
        activity: list[str] = list(self.pre_activity)
        final_parts: list[str] = []
        resp: dict[str, Any] = {}
        max_iter = self.settings.max_tool_iterations
        for iteration in range(max_iter + 1):
            allow_internal = iteration < max_iter
            resp = await self._upstream_chat(allow_internal)
            msg = (resp.get("choices") or [{}])[0].get("message") or {}
            content = msg.get("content") or ""
            calls = [
                _ToolCall(
                    id=tc.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                    name=tc["function"]["name"],
                    arguments=tc["function"].get("arguments") or "",
                )
                for tc in msg.get("tool_calls") or []
                if tc.get("function")
            ]
            final_parts.append(content)
            raw = resp.pop(RAW_KEY, None)
            internal = self.internal_names
            if calls and allow_internal and all(c.name in internal for c in calls):
                activity += await self._run_tools(calls, content, raw)
                continue
            break

        resp["model"] = self.requested_model
        try:
            msg = resp["choices"][0]["message"]
            prefix = activity_block(activity) if activity and self.settings.show_tool_activity else ""
            msg["content"] = prefix + "".join(final_parts[:-1]) + (msg.get("content") or "")
        except (KeyError, IndexError):
            pass
        self.persist("".join(final_parts))
        return resp

    # --------------------------------------------------------------- Speichern
    def persist(self, assistant_text: str) -> None:
        if self.incognito:
            return
        user_msgs = [m for m in self.incoming if m.get("role") == "user"]
        if not user_msgs:
            return
        user_text = text_of(user_msgs[-1].get("content")).strip()
        if not user_text:
            return
        store = self.s.store
        try:
            store.upsert_conversation(self.conversation_id, self.mode)
            store.upsert_turn(
                self.conversation_id,
                self.turn_index,
                user_text,
                assistant_text.strip(),
                self.upstream_model,
                self.tool_log,
            )
            # Dreaming nach Inaktivität (jede neue Nachricht schiebt den Zeitpunkt nach hinten)
            store.enqueue_job(
                "dream_conversation",
                {"conversation_id": self.conversation_id},
                delay_seconds=self.settings.dream_idle_minutes * 60,
                dedupe_key=f"dream:{self.conversation_id}",
            )
            if self.ctx.memory_written:
                store.enqueue_job("index_embeddings", {"changes": 1}, dedupe_key="index")
            self.s.worker.notify()
        except Exception:
            log.exception("Konnte Turn nicht speichern")

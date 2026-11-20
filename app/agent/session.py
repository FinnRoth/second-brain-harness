"""Kern einer Chat-Anfrage: Kontext anreichern → Agent-Loop mit Tools → typisierte Events.

`ChatSession.run()` liefert Events, die von zwei Frontends konsumiert werden:
- der eigenen App (app/agent/runs.py) – Freigaben laufen dort über Buttons (approver)
- dem OpenAI-kompatiblen Adapter für Open WebUI (app/agent/openai_adapter.py) –
  ohne approver werden Freigaben vorgemerkt und per „ok“ in der nächsten Nachricht erteilt

Event-Typen:
  text {delta} · reasoning {delta} · tool_start {call_id, name, title, source, kind, args}
  approval_required {call_id, approval_id, …} · approval_resolved {call_id, approval_id, decision}
  tool_end {call_id, result, is_error, description} · tools_done {descriptions}
  activity {lines} · client_tool_calls {calls} · done {finish_reason, usage} · error {message}
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable

from ..llm import UpstreamError
from ..memory.retrieval import text_of
from ..responses_api import RAW_KEY, strip_private
from ..tools.base import ToolContext, ToolResult
from ..tools.memory import describe_call
from ..tools.permissions import MODE_DESCRIPTIONS, classify_reply
from ..tools.registry import TOOL_SEARCH, ToolRegistry, ToolSet, short_args

if TYPE_CHECKING:
    from ..services import Services

log = logging.getLogger(__name__)

# Parameter, die 1:1 an LiteLLM durchgereicht werden. Alles andere wird verworfen.
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

# approver(approval_id, info) -> "allow" | "deny"
Approver = Callable[[str, dict[str, Any]], Awaitable[str]]


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


class ChatSession:
    def __init__(
        self,
        services: "Services",
        *,
        messages: list[dict[str, Any]],
        model: str,
        conversation_id: str,
        incognito: bool = False,
        permission_mode: str | None = None,
        params: dict[str, Any] | None = None,
        client_tools: list[dict[str, Any]] | None = None,
        approver: Approver | None = None,
        allowed_tools: set[str] | None = None,
        conv_mode: str | None = None,
        mode_changed: str | None = None,
    ) -> None:
        self.s = services
        self.settings = services.settings
        self.upstream_model = model
        self.conversation_id = conversation_id
        self.incognito = incognito
        self.conv_mode = conv_mode or ("incognito" if incognito else "brain")
        self.incoming = messages
        self.turn_index = sum(1 for m in messages if m.get("role") == "user")
        self.params = {k: v for k, v in (params or {}).items() if k in PASSTHROUGH_PARAMS and v is not None}
        self.client_tools = client_tools or []
        self.approver = approver
        self.mode_changed = mode_changed

        self.registry: ToolRegistry = services.tools
        self.ctx = ToolContext(
            conversation_id=conversation_id,
            incognito=incognito,
            permission_mode=permission_mode or self.settings.permission_mode,
            allowed_tools=set(allowed_tools or ()),
        )
        self.toolset: ToolSet = ToolSet({}, {})
        self.tool_log: list[dict[str, Any]] = []
        self.pre_activity: list[str] = []
        self.messages: list[dict[str, Any]] = []
        self.final_parts: list[str] = []
        self.completed = False
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

    @property
    def final_text(self) -> str:
        return "".join(self.final_parts)

    # ---------------------------------------------------------------- Vorbereitung
    async def prepare(self) -> None:
        self._refresh_tools()
        approvals_note = "" if self.approver else await self._handle_pending_approvals()

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
        has_external = any(r.spec.source.startswith("mcp:") for r in self.toolset.tools.values()) or bool(
            self.toolset.deferred
        )
        if has_external or self.ctx.permission_mode != self.settings.permission_mode or self.mode_changed:
            mode = self.ctx.permission_mode
            desc = MODE_DESCRIPTIONS[mode]
            if self.approver and mode == "ask":
                desc = (
                    "Freigabe-Modus „ask“: Lesende Tools laufen sofort. Bei Aktionen, die etwas verändern, "
                    "fragt der Harness den Nutzer per Button um Erlaubnis – rufe das Tool einfach auf."
                )
            extra.append(f"Freigabe-Modus: {mode}. {desc}")
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
        """Open-WebUI-Weg: kurze Zustimmung führt aus, Ablehnung verwirft, alles andere lässt verfallen."""
        store = self.s.store
        pending = store.pending_approvals(self.conversation_id)
        if not pending:
            return ""
        users = [m for m in self.incoming if m.get("role") == "user"]
        verdict = classify_reply(text_of(users[-1].get("content")) if users else "")
        if verdict == "yes":
            lines = []
            for a in pending:
                result = await self._call_tool(a["tool_name"], a["arguments"])
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
    def tool_info(self, name: str) -> dict[str, Any]:
        rt = self.toolset.tools.get(name) or self.toolset.deferred.get(name)
        if rt is None:
            return {"title": name, "source": "", "kind": ""}
        return {"title": rt.spec.title or name, "source": rt.spec.source, "kind": rt.spec.kind}

    def _describe(self, name: str, args: dict[str, Any]) -> str:
        rt = self.toolset.tools.get(name) or self.toolset.deferred.get(name)
        if rt and rt.spec.source.startswith("mcp:"):
            return f"🔧 {rt.spec.source[4:]} · {rt.spec.title} {short_args(args, 80)}"
        if name == TOOL_SEARCH:
            return f"🔎 Tools gesucht: „{args.get('query', '')}“"
        return describe_call(name, args)

    async def _call_tool(self, name: str, args: dict[str, Any]) -> ToolResult:
        if name == TOOL_SEARCH:
            return self.registry.search(self.toolset, str(args.get("query", "")), self.ctx)
        rt = self.toolset.tools.get(name) or self.toolset.deferred.get(name)
        if rt is None or rt.provider is None:
            return ToolResult(f"Fehler: Tool '{name}' ist im aktuellen Modus nicht verfügbar.", is_error=True)
        log.info("Tool-Aufruf %s(%s)", name, short_args(args, 200))
        return await rt.provider.call(name, args, self.ctx)

    async def _run_tools(
        self, calls: list[_ToolCall], content: str, raw: list[dict[str, Any]] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
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
            info = self.tool_info(c.name)
            yield {"type": "tool_start", "call_id": c.id, "name": c.name, "args": args or {}, **info}
            if args is None:
                result = ToolResult("Fehler: Argumente sind kein gültiges JSON-Objekt.", is_error=True)
                desc = f"⚠️ {c.name}: ungültige Argumente"
                args = {}
            else:
                rt = self.toolset.tools.get(c.name)
                desc = self._describe(c.name, args)
                if rt is not None and rt.decision == "ask":
                    if self.approver:
                        approval_id = uuid.uuid4().hex
                        yield {
                            "type": "approval_required",
                            "call_id": c.id,
                            "approval_id": approval_id,
                            "name": c.name,
                            "args": args,
                            "description": desc,
                            **info,
                        }
                        decision = await self.approver(approval_id, {"name": c.name, "args": args, **info})
                        yield {
                            "type": "approval_resolved",
                            "call_id": c.id,
                            "approval_id": approval_id,
                            "decision": decision,
                        }
                        if decision == "allow":
                            result = await self._call_tool(c.name, args)
                            self._refresh_tools()  # „für diesen Chat erlauben“ kann Entscheidungen ändern
                        else:
                            result = ToolResult(
                                "Der Nutzer hat diese Aktion abgelehnt. Sie wurde nicht ausgeführt. "
                                "Frag nach, was stattdessen gewünscht ist.",
                                is_error=True,
                            )
                            desc = f"🚫 Abgelehnt: {desc}"
                    else:
                        aid = self.s.store.create_approval(self.conversation_id, c.name, args, desc)
                        user = self.settings.user_name
                        result = ToolResult(
                            f"Diese Aktion braucht die Freigabe von {user} und wurde vorgemerkt (Freigabe #{aid}), "
                            "aber NICHT ausgeführt. Erkläre kurz, was du tun möchtest und warum, und bitte um "
                            f"Bestätigung: {user} antwortet mit „ok“ zum Ausführen oder „nein“ zum Abbrechen. "
                            "Rufe das Tool nicht erneut auf."
                        )
                        desc = f"⏸️ Wartet auf Freigabe: {desc}"
                else:
                    result = await self._call_tool(c.name, args)
            self.messages.append({"role": "tool", "tool_call_id": c.id, "content": result.text})
            self.tool_log.append({"name": c.name, "arguments": args, "result": result.text[:500]})
            descriptions.append(desc)
            yield {
                "type": "tool_end",
                "call_id": c.id,
                "result": result.text[:6000],
                "is_error": result.is_error,
                "description": desc,
            }
        self._refresh_tools()  # tool_search kann neue Tools freigeschaltet haben
        yield {"type": "tools_done", "descriptions": descriptions}

    # ------------------------------------------------------------------ Agent-Loop
    async def run(self) -> AsyncIterator[dict[str, Any]]:
        reasoning_sent = False
        usage: dict[str, Any] | None = None
        try:
            if self.pre_activity:
                yield {"type": "activity", "lines": list(self.pre_activity)}
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
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                    if reasoning:
                        if reasoning_sent and not iteration_reasoning:
                            reasoning = "\n\n" + reasoning
                        reasoning_sent = iteration_reasoning = True
                        yield {"type": "reasoning", "delta": reasoning}
                    if delta.get("content"):
                        iteration_text.append(delta["content"])
                        self.final_parts.append(delta["content"])  # live, damit ein Abbruch nichts verliert
                        yield {"type": "text", "delta": delta["content"]}

                calls = acc.result()
                text = "".join(iteration_text)
                internal = self.internal_names
                if calls and allow_internal and all(c.name in internal for c in calls):
                    async for ev in self._run_tools(calls, text, raw):
                        yield ev
                    continue
                if calls:
                    # Tools des Clients (Open WebUI native function calling) → an den Client durchreichen
                    yield {
                        "type": "client_tool_calls",
                        "calls": [
                            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                            for c in calls
                            if c.name not in internal
                        ],
                    }
                    finish_reason = "tool_calls"
                self.completed = True
                yield {"type": "done", "finish_reason": finish_reason or "stop", "usage": usage}
                return
        except UpstreamError as e:
            log.error("Upstream-Fehler: %s", e)
            yield {"type": "error", "message": e.message[:1000]}

    # --------------------------------------------------------------- Speichern
    def persist(self, assistant_text: str | None = None) -> None:
        """Turn fürs Gedächtnis speichern und Dreaming einplanen."""
        if self.incognito:
            return
        assistant_text = self.final_text if assistant_text is None else assistant_text
        user_msgs = [m for m in self.incoming if m.get("role") == "user"]
        if not user_msgs:
            return
        user_text = text_of(user_msgs[-1].get("content")).strip()
        if not user_text:
            return
        store = self.s.store
        try:
            store.upsert_conversation(self.conversation_id, self.conv_mode)
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

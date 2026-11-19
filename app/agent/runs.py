"""Runs für die eigene App.

Eine Antwort läuft als Hintergrund-Task („Run“), unabhängig von der HTTP-Verbindung:
- Events werden gepuffert und können jederzeit (auch nach Neuladen) abgespielt werden
- Freigaben pausieren den Run, bis der Nutzer per Button entscheidet
- Stopp bricht den Task ab; das bisher Generierte wird gespeichert

Inkognito-Chats leben nur im Speicher und verschwinden beim Neustart.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, AsyncIterator

from ..llm import message_content
from .session import ChatSession

if TYPE_CHECKING:
    from ..services import Services

log = logging.getLogger(__name__)

APPROVAL_TIMEOUT = 30 * 60
FINISHED_RUN_TTL = 15 * 60
TITLE_PROMPT = (
    "Du erzeugst kurze, prägnante Chat-Titel auf Deutsch (max. 6 Wörter, keine Anführungszeichen, "
    "kein Punkt am Ende). Antworte nur mit dem Titel."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ Nachrichtenteile
class PartsBuilder:
    """Baut aus Events die geordneten Teile einer Antwort (Denkprozess, Text, Tools …)."""

    def __init__(self) -> None:
        self.parts: list[dict[str, Any]] = []

    def _tool(self, call_id: str) -> dict[str, Any] | None:
        for p in reversed(self.parts):
            if p.get("type") == "tool" and p.get("call_id") == call_id:
                return p
        return None

    def apply(self, ev: dict[str, Any]) -> None:
        t = ev.get("type")
        last = self.parts[-1] if self.parts else None
        if t in ("text", "reasoning"):
            if last and last["type"] == t:
                last["text"] += ev["delta"]
            else:
                self.parts.append({"type": t, "text": ev["delta"].lstrip("\n") if t == "reasoning" else ev["delta"]})
        elif t == "tool_start":
            self.parts.append(
                {
                    "type": "tool",
                    "call_id": ev["call_id"],
                    "name": ev["name"],
                    "title": ev.get("title") or ev["name"],
                    "source": ev.get("source", ""),
                    "kind": ev.get("kind", ""),
                    "args": ev.get("args") or {},
                    "status": "running",
                }
            )
        elif t == "approval_required":
            if p := self._tool(ev["call_id"]):
                p["status"] = "approval"
                p["approval_id"] = ev["approval_id"]
        elif t == "approval_resolved":
            if p := self._tool(ev["call_id"]):
                p["status"] = "running" if ev["decision"] == "allow" else "denied"
                p["approved"] = ev["decision"] == "allow"
        elif t == "tool_end":
            if p := self._tool(ev["call_id"]):
                if p["status"] != "denied":
                    p["status"] = "error" if ev.get("is_error") else "done"
                p["result"] = ev.get("result", "")
        elif t == "activity":
            for line in ev.get("lines") or []:
                self.parts.append({"type": "notice", "text": line})
        elif t == "error":
            self.parts.append({"type": "error", "text": ev.get("message", "Fehler")})


# --------------------------------------------------------------------------- Chats
class ChatRepo:
    """Einheitlicher Zugriff auf App-Chats – dauerhaft (SQLite) oder inkognito (Speicher)."""

    def __init__(self, services: "Services") -> None:
        self.s = services
        self._incognito: dict[str, dict[str, Any]] = {}
        self._ids = itertools.count(10_000_000)

    @staticmethod
    def is_incognito(chat_id: str) -> bool:
        return chat_id.startswith("inc-")

    def create(self, incognito: bool, model: str | None, permission_mode: str | None) -> dict[str, Any]:
        chat_id = ("inc-" if incognito else "app-") + uuid.uuid4().hex[:16]
        if incognito:
            self._incognito[chat_id] = {
                "meta": {
                    "id": chat_id,
                    "title": None,
                    "model": model,
                    "permission_mode": permission_mode,
                    "pinned": False,
                    "source": "app",
                    "created_at": _now(),
                    "updated_at": _now(),
                    "incognito": True,
                    "allowed_tools": [],
                },
                "messages": [],
            }
        else:
            self.s.store.create_conversation(chat_id, model, permission_mode)
        return self.get(chat_id)

    def get(self, chat_id: str) -> dict[str, Any] | None:
        if self.is_incognito(chat_id):
            c = self._incognito.get(chat_id)
            return dict(c["meta"]) if c else None
        conv = self.s.store.get_conversation(chat_id)
        if not conv or conv["mode"] != "brain":
            return None
        return {
            "id": conv["id"],
            "title": conv.get("title"),
            "model": conv.get("model"),
            "permission_mode": conv.get("permission_mode"),
            "pinned": bool(conv.get("pinned")),
            "source": conv.get("source"),
            "created_at": conv["created_at"],
            "updated_at": conv["updated_at"],
            "incognito": False,
            "allowed_tools": sorted(self.s.store.get_allowed_tools(chat_id)),
        }

    def list(self, q: str | None = None) -> list[dict[str, Any]]:
        items = []
        for c in self._incognito.values():
            m = c["meta"]
            if q and q.lower() not in (m.get("title") or "").lower():
                continue
            items.append({**m, "title": m.get("title") or "Inkognito-Chat"})
        items += [{**c, "incognito": False} for c in self.s.store.list_chats(q)]
        items.sort(key=lambda c: (not c.get("pinned"), c["updated_at"]), reverse=False)
        pinned = [c for c in items if c.get("pinned")]
        rest = sorted((c for c in items if not c.get("pinned")), key=lambda c: c["updated_at"], reverse=True)
        return pinned + rest

    def update(self, chat_id: str, **fields: Any) -> None:
        if self.is_incognito(chat_id):
            meta = self._incognito[chat_id]["meta"]
            for k, v in fields.items():
                if v is not None and k in ("title", "model", "permission_mode", "pinned"):
                    meta[k] = v
            return
        self.s.store.update_conversation(chat_id, **fields)

    def delete(self, chat_id: str) -> bool:
        if self.is_incognito(chat_id):
            return self._incognito.pop(chat_id, None) is not None
        return self.s.store.delete_conversation(chat_id)

    def messages(self, chat_id: str) -> list[dict[str, Any]]:
        if self.is_incognito(chat_id):
            return [dict(m) for m in self._incognito[chat_id]["messages"]]
        self.s.store.import_turns_as_messages(chat_id)
        return self.s.store.list_messages(chat_id)

    def add_message(self, chat_id: str, role: str, content: str, parts: list[dict[str, Any]], **kw: Any) -> int:
        if self.is_incognito(chat_id):
            mid = next(self._ids)
            c = self._incognito[chat_id]
            c["messages"].append(
                {
                    "id": mid,
                    "conversation_id": chat_id,
                    "role": role,
                    "content": content,
                    "parts": parts,
                    "model": kw.get("model"),
                    "status": kw.get("status", "done"),
                    "usage": kw.get("usage"),
                    "created_at": _now(),
                }
            )
            c["meta"]["updated_at"] = _now()
            return mid
        return self.s.store.add_message(chat_id, role, content, parts, **kw)

    def delete_from(self, chat_id: str, message_id: int) -> None:
        if self.is_incognito(chat_id):
            c = self._incognito[chat_id]
            c["messages"] = [m for m in c["messages"] if m["id"] < message_id]
            return
        self.s.store.delete_messages_from(chat_id, message_id)

    def allow_tool(self, chat_id: str, tool: str) -> None:
        if self.is_incognito(chat_id):
            allowed = self._incognito[chat_id]["meta"]["allowed_tools"]
            if tool not in allowed:
                allowed.append(tool)
            return
        self.s.store.allow_tool_for_conversation(chat_id, tool)


def to_llm_message(m: dict[str, Any]) -> dict[str, Any] | None:
    """Gespeicherte Nachricht → OpenAI-Nachricht (inkl. Anhängen)."""
    if m["role"] == "assistant":
        if not m.get("content") and m.get("status") != "done":
            return None
        return {"role": "assistant", "content": m.get("content") or ""}
    if m["role"] != "user":
        return None
    text = m.get("content") or ""
    files = [p for p in m.get("parts") or [] if p.get("type") == "file"]
    images = [p for p in m.get("parts") or [] if p.get("type") == "image"]
    if files:
        blocks = [f'<datei name="{f.get("name", "datei")}">\n{f.get("text", "")}\n</datei>' for f in files]
        text = (text + "\n\n" + "\n\n".join(blocks)).strip()
    if images:
        return {
            "role": "user",
            "content": [{"type": "text", "text": text or "(Bild)"}]
            + [{"type": "image_url", "image_url": {"url": i["url"]}} for i in images],
        }
    return {"role": "user", "content": text}


# ---------------------------------------------------------------------------- Runs
@dataclass
class Run:
    id: str
    chat_id: str
    model: str
    events: list[dict[str, Any]] = field(default_factory=list)
    done: bool = False
    status: str = "running"  # running | waiting | done | error | cancelled
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    task: asyncio.Task | None = None
    session: ChatSession | None = None
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    pending_info: dict[str, dict[str, Any]] = field(default_factory=dict)
    _changed: asyncio.Event = field(default_factory=asyncio.Event)

    def publish(self, ev: dict[str, Any]) -> None:
        self.events.append({"seq": len(self.events), **ev})
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    def finish(self, status: str) -> None:
        self.status = status
        self.done = True
        self.finished_at = time.time()
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    async def subscribe(self, after: int = -1, keepalive: float = 15.0) -> AsyncIterator[dict[str, Any] | None]:
        """Alle Events ab `after+1`, danach live. `None` = Keepalive."""
        i = after + 1
        while True:
            while i < len(self.events):
                yield self.events[i]
                i += 1
            if self.done:
                return
            waiter = self._changed
            try:
                await asyncio.wait_for(waiter.wait(), timeout=keepalive)
            except asyncio.TimeoutError:
                yield None


class RunManager:
    def __init__(self, services: "Services") -> None:
        self.s = services
        self.repo = ChatRepo(services)
        self.runs: dict[str, Run] = {}
        self.by_chat: dict[str, str] = {}
        self.approvals: dict[str, Run] = {}

    def active(self, chat_id: str) -> Run | None:
        rid = self.by_chat.get(chat_id)
        run = self.runs.get(rid) if rid else None
        return run if run and not run.done else None

    def _cleanup(self) -> None:
        now = time.time()
        for rid, run in list(self.runs.items()):
            if run.done and run.finished_at and now - run.finished_at > FINISHED_RUN_TTL:
                self.runs.pop(rid, None)
                if self.by_chat.get(run.chat_id) == rid:
                    self.by_chat.pop(run.chat_id, None)

    def start(self, chat_id: str, model: str, params: dict[str, Any]) -> Run:
        self._cleanup()
        run = Run(id=uuid.uuid4().hex, chat_id=chat_id, model=model)
        self.runs[run.id] = run
        self.by_chat[chat_id] = run.id
        run.task = asyncio.create_task(self._execute(run, params), name=f"run:{run.id}")
        return run

    def cancel(self, run_id: str) -> bool:
        run = self.runs.get(run_id)
        if not run or run.done or not run.task:
            return False
        run.task.cancel()
        return True

    async def shutdown(self) -> None:
        for run in list(self.runs.values()):
            if run.task and not run.done:
                run.task.cancel()
        tasks = [r.task for r in self.runs.values() if r.task]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------ Freigaben
    def _approver(self, run: Run):
        async def approve(approval_id: str, info: dict[str, Any]) -> str:
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            run.pending[approval_id] = fut
            run.pending_info[approval_id] = info
            self.approvals[approval_id] = run
            run.status = "waiting"
            try:
                return await asyncio.wait_for(fut, timeout=APPROVAL_TIMEOUT)
            except asyncio.TimeoutError:
                return "deny"
            finally:
                run.pending.pop(approval_id, None)
                run.pending_info.pop(approval_id, None)
                self.approvals.pop(approval_id, None)
                run.status = "running"

        return approve

    def resolve(self, approval_id: str, decision: str, scope: str = "once") -> bool:
        run = self.approvals.get(approval_id)
        fut = run.pending.get(approval_id) if run else None
        if not run or not fut or fut.done():
            return False
        info = run.pending_info.get(approval_id, {})
        name = info.get("name")
        if decision == "allow" and name:
            if scope == "chat":
                self.repo.allow_tool(run.chat_id, name)
                if run.session:
                    run.session.ctx.allowed_tools.add(name)
            elif scope == "always":
                self.s.store.set_tool_policy(name, "allow")
        fut.set_result("allow" if decision == "allow" else "deny")
        return True

    # --------------------------------------------------------------- Ausführung
    async def _execute(self, run: Run, params: dict[str, Any]) -> None:
        repo = self.repo
        chat = repo.get(run.chat_id) or {}
        history = [m for m in (to_llm_message(m) for m in repo.messages(run.chat_id)) if m]
        builder = PartsBuilder()
        status, usage = "done", None
        session = ChatSession(
            self.s,
            messages=history,
            model=run.model,
            conversation_id=run.chat_id,
            incognito=bool(chat.get("incognito")),
            permission_mode=chat.get("permission_mode"),
            params=params,
            approver=self._approver(run),
            allowed_tools=set(chat.get("allowed_tools") or []),
        )
        run.session = session
        try:
            await session.prepare()
            run.publish({"type": "start", "model": run.model, "permission_mode": session.ctx.permission_mode})
            async for ev in session.run():
                if ev["type"] in ("tools_done",):
                    continue
                builder.apply(ev)
                run.publish(ev)
                if ev["type"] == "done":
                    usage = ev.get("usage")
                elif ev["type"] == "error":
                    status = "error"
        except asyncio.CancelledError:
            status = "cancelled"
        except Exception as e:  # Fehler sichtbar machen statt Run hängen zu lassen
            log.exception("Run %s fehlgeschlagen", run.id)
            status = "error"
            ev = {"type": "error", "message": f"{type(e).__name__}: {e}"}
            builder.apply(ev)
            run.publish(ev)
        for fut in run.pending.values():
            if not fut.done():
                fut.cancel()
        try:
            text = session.final_text
            mid = repo.add_message(
                run.chat_id, "assistant", text, builder.parts, model=run.model, status=status, usage=usage
            )
            if session.completed or (status == "cancelled" and text.strip()):
                session.persist(text)
            run.publish({"type": "saved", "message_id": mid, "status": status})
            if status == "done" and not (repo.get(run.chat_id) or {}).get("title"):
                title = await self._title(history, text)
                if title:
                    repo.update(run.chat_id, title=title)
                    run.publish({"type": "title", "title": title})
        except Exception:
            log.exception("Run %s: Speichern fehlgeschlagen", run.id)
        finally:
            run.publish({"type": "end", "status": status})
            run.finish(status)

    async def _title(self, history: list[dict[str, Any]], answer: str) -> str | None:
        users = [m for m in history if m["role"] == "user"]
        if not users:
            return None
        content = users[0]["content"]
        text = content if isinstance(content, str) else " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        try:
            resp = await self.s.llm.chat(
                {
                    "model": self.s.settings.effective_background_model,
                    "messages": [
                        {"role": "system", "content": TITLE_PROMPT},
                        {"role": "user", "content": f"Nachricht: {text[:1500]}\n\nAntwort: {answer[:800]}"},
                    ],
                }
            )
        except Exception as e:
            log.warning("Titel konnte nicht erzeugt werden: %s", e)
            return None
        title = message_content(resp).strip().strip("\"'„“«»").splitlines()[0] if message_content(resp).strip() else ""
        return title[:80] or None

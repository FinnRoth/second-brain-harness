"""API der eigenen App: Chats, Nachrichten, Runs (Event-Stream), Freigaben, Tool-Regeln."""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..tools.base import ToolContext
from ..tools.permissions import PERMISSION_MODES
from .deps import get_services, require_api_key

router = APIRouter(prefix="/api", tags=["app"], dependencies=[Depends(require_api_key)])

REASONING_EFFORTS = ["minimal", "low", "medium", "high"]
MAX_TEXT_ATTACHMENT = 300_000
MAX_IMAGE_BYTES = 10 * 1024 * 1024


class Attachment(BaseModel):
    name: str
    kind: Literal["text", "image"]
    text: str | None = None  # für kind=text
    url: str | None = None  # data:-URL für kind=image


class ChatCreate(BaseModel):
    incognito: bool = False
    model: str | None = None
    permission_mode: str | None = None


class ChatPatch(BaseModel):
    title: str | None = None
    pinned: bool | None = None
    model: str | None = None
    permission_mode: str | None = None


class SendMessage(BaseModel):
    content: str = ""
    attachments: list[Attachment] = Field(default_factory=list)
    model: str | None = None
    permission_mode: str | None = None
    reasoning_effort: str | None = None
    edit_message_id: int | None = None


class Regenerate(BaseModel):
    model: str | None = None
    reasoning_effort: str | None = None


class ApprovalDecision(BaseModel):
    decision: Literal["allow", "deny"]
    scope: Literal["once", "chat", "always"] = "once"


class PolicyUpdate(BaseModel):
    policy: Literal["allow", "ask", "deny"] | None = None


def _runs(request: Request):
    return get_services(request).runs


def _chat_or_404(request: Request, chat_id: str) -> dict[str, Any]:
    chat = _runs(request).repo.get(chat_id)
    if not chat:
        raise HTTPException(404, "Chat nicht gefunden")
    return chat


def _check_mode(mode: str | None) -> None:
    if mode is not None and mode not in PERMISSION_MODES:
        raise HTTPException(400, f"Unbekannter Modus {mode}")


# ---------------------------------------------------------------------- Config
@router.get("/config")
async def config(request: Request):
    s = get_services(request).settings
    return {
        "user_name": s.user_name,
        "models": s.chat_model_list,
        "default_model": s.chat_model_list[0] if s.chat_model_list else None,
        "permission_modes": list(PERMISSION_MODES),
        "default_permission_mode": s.permission_mode,
        "reasoning_efforts": REASONING_EFFORTS,
    }


# ----------------------------------------------------------------------- Chats
@router.get("/chats")
async def list_chats(request: Request, q: str | None = None):
    runs = _runs(request)
    out = []
    for c in runs.repo.list(q):
        out.append({**c, "running": runs.active(c["id"]) is not None})
    return out


@router.post("/chats", status_code=201)
async def create_chat(request: Request, body: ChatCreate):
    _check_mode(body.permission_mode)
    return _runs(request).repo.create(body.incognito, body.model, body.permission_mode)


@router.get("/chats/{chat_id}")
async def get_chat(request: Request, chat_id: str):
    runs = _runs(request)
    chat = _chat_or_404(request, chat_id)
    run = runs.active(chat_id)
    return {
        **chat,
        "messages": runs.repo.messages(chat_id),
        "active_run": {"id": run.id, "model": run.model} if run else None,
    }


@router.patch("/chats/{chat_id}")
async def patch_chat(request: Request, chat_id: str, body: ChatPatch):
    _chat_or_404(request, chat_id)
    _check_mode(body.permission_mode)
    data = body.model_dump()
    if data.get("title") is not None:
        data["title"] = data["title"].strip()[:120] or None
    _runs(request).repo.update(chat_id, **data)
    return _runs(request).repo.get(chat_id)


@router.delete("/chats/{chat_id}")
async def delete_chat(request: Request, chat_id: str):
    runs = _runs(request)
    if run := runs.active(chat_id):
        runs.cancel(run.id)
    if not runs.repo.delete(chat_id):
        raise HTTPException(404, "Chat nicht gefunden")
    return {"deleted": chat_id}


def _user_parts(body: SendMessage) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    if body.content:
        parts.append({"type": "text", "text": body.content})
    for a in body.attachments:
        if a.kind == "text":
            text = (a.text or "")[:MAX_TEXT_ATTACHMENT]
            parts.append({"type": "file", "name": a.name, "text": text, "chars": len(a.text or "")})
        else:
            if not a.url or not a.url.startswith("data:image/"):
                raise HTTPException(400, f"Bild {a.name}: ungültige Daten")
            if len(a.url) * 3 // 4 > MAX_IMAGE_BYTES:
                raise HTTPException(400, f"Bild {a.name} ist größer als 10 MB")
            parts.append({"type": "image", "name": a.name, "url": a.url})
    return parts


def _params(effort: str | None) -> dict[str, Any]:
    if effort:
        if effort not in REASONING_EFFORTS:
            raise HTTPException(400, "Unbekannter Denkaufwand")
        return {"reasoning_effort": effort}
    return {}


def _model(request: Request, chat: dict[str, Any], requested: str | None) -> str:
    s = get_services(request).settings
    model = requested or chat.get("model") or (s.chat_model_list[0] if s.chat_model_list else None)
    if not model:
        raise HTTPException(400, "Kein Modell konfiguriert (CHAT_MODELS)")
    return model


@router.post("/chats/{chat_id}/messages", status_code=202)
async def send_message(request: Request, chat_id: str, body: SendMessage):
    runs = _runs(request)
    chat = _chat_or_404(request, chat_id)
    if runs.active(chat_id):
        raise HTTPException(409, "In diesem Chat läuft bereits eine Antwort")
    if not body.content.strip() and not body.attachments:
        raise HTTPException(400, "Leere Nachricht")
    _check_mode(body.permission_mode)
    model = _model(request, chat, body.model)
    params = _params(body.reasoning_effort)
    parts = _user_parts(body)
    if body.edit_message_id is not None:
        runs.repo.delete_from(chat_id, body.edit_message_id)
    runs.repo.update(chat_id, model=model, permission_mode=body.permission_mode)
    mid = runs.repo.add_message(chat_id, "user", body.content, parts)
    run = runs.start(chat_id, model, params)
    return {"run_id": run.id, "user_message_id": mid}


@router.post("/chats/{chat_id}/regenerate", status_code=202)
async def regenerate(request: Request, chat_id: str, body: Regenerate):
    runs = _runs(request)
    chat = _chat_or_404(request, chat_id)
    if runs.active(chat_id):
        raise HTTPException(409, "In diesem Chat läuft bereits eine Antwort")
    messages = runs.repo.messages(chat_id)
    last_user = max((i for i, m in enumerate(messages) if m["role"] == "user"), default=None)
    if last_user is None:
        raise HTTPException(400, "Keine Nachricht zum Neu-Generieren")
    if last_user + 1 < len(messages):
        runs.repo.delete_from(chat_id, messages[last_user + 1]["id"])
    model = _model(request, chat, body.model)
    runs.repo.update(chat_id, model=model)
    run = runs.start(chat_id, model, _params(body.reasoning_effort))
    return {"run_id": run.id}


# ------------------------------------------------------------------------ Runs
@router.get("/runs/{run_id}/events")
async def run_events(request: Request, run_id: str, after: int = -1):
    run = _runs(request).runs.get(run_id)
    if not run:
        raise HTTPException(404, "Run nicht gefunden (evtl. abgeschlossen)")

    async def gen():
        async for ev in run.subscribe(after):
            if await request.is_disconnected():
                return
            if ev is None:
                yield ": keepalive\n\n"
            else:
                yield f"id: {ev['seq']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@router.post("/runs/{run_id}/cancel")
async def cancel_run(request: Request, run_id: str):
    if not _runs(request).cancel(run_id):
        raise HTTPException(404, "Kein laufender Run")
    return {"cancelled": run_id}


@router.post("/approvals/{approval_id}")
async def decide_approval(request: Request, approval_id: str, body: ApprovalDecision):
    if not _runs(request).resolve(approval_id, body.decision, body.scope):
        raise HTTPException(404, "Freigabe nicht (mehr) offen")
    return {"ok": True}


# ----------------------------------------------------------------------- Tools
@router.get("/tools")
async def tools(request: Request):
    """Alle Tools mit Herkunft, Art und Regel – für die Tool-Übersicht der App."""
    s = get_services(request)
    overrides = s.store.tool_policies()
    specs = s.tools.all_specs(ToolContext(conversation_id="overview", permission_mode="auto"))
    return [
        {
            "name": spec.name,
            "title": spec.title or spec.name,
            "description": spec.description,
            "source": spec.source,
            "kind": spec.kind,
            "policy": spec.policy,
            "override": overrides.get(spec.name),
        }
        for spec in specs
    ]


@router.put("/tools/{name}/policy")
async def set_policy(request: Request, name: str, body: PolicyUpdate):
    get_services(request).store.set_tool_policy(name, body.policy)
    return {"name": name, "policy": body.policy}

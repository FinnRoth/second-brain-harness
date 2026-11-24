"""API der eigenen App: Chats, Nachrichten, Runs (Event-Stream), Freigaben, Tool-Regeln."""

from __future__ import annotations

import json
from typing import Any, Literal

import asyncio
import re

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from .. import bundles
from ..tools.base import ToolContext
from ..tools.permissions import PERMISSION_MODES
from .deps import get_services, require_api_key

router = APIRouter(prefix="/api", tags=["app"], dependencies=[Depends(require_api_key)])

REASONING_EFFORTS = ["minimal", "low", "medium", "high"]
MAX_TEXT_ATTACHMENT = 300_000
MAX_IMAGE_BYTES = 10 * 1024 * 1024


class Attachment(BaseModel):
    name: str
    kind: Literal["text", "image", "file"]
    text: str | None = None  # für kind=text
    url: str | None = None  # data:-URL für kind=image
    file_id: str | None = None  # für kind=file (vorher über /api/files hochgeladen)


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


class SettingsUpdate(BaseModel):
    custom_instructions: str


class ReferenceIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    content: str = ""
    mode: Literal["always", "on_demand"] = "on_demand"
    enabled: bool = True


class ReferencePatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    content: str | None = None
    mode: Literal["always", "on_demand"] | None = None
    enabled: bool | None = None


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
    messages = runs.repo.messages(chat_id)
    return {
        **chat,
        "messages": messages,
        "active_run": {"id": run.id, "model": run.model} if run else None,
        "usage": _chat_usage(request, chat, messages),
    }


def _chat_usage(request: Request, chat: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, Any]:
    s = get_services(request)
    prompt = completion = 0
    cost, priced, unpriced = 0.0, 0, 0
    context_tokens, model = 0, chat.get("model")
    for m in messages:
        u = m.get("usage") if m["role"] == "assistant" else None
        if not u:
            continue
        prompt += u.get("prompt_tokens", 0)
        completion += u.get("completion_tokens", 0)
        context_tokens = u.get("context_tokens") or (u.get("prompt_tokens", 0) + u.get("completion_tokens", 0))
        model = m.get("model") or model
        if u.get("cost") is None:
            unpriced += 1
        else:
            cost += u["cost"]
            priced += 1
    model = model or (s.settings.chat_model_list[0] if s.settings.chat_model_list else None)
    info = s.catalog.info(model)
    return {
        "model": model,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "cost": round(cost, 6) if priced else None,
        "partially_priced": bool(priced and unpriced),
        "context_tokens": context_tokens,
        "context_window": info.get("context_window"),
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
    get_services(request).documents.delete_for_chat(chat_id)
    return {"deleted": chat_id}


def _user_parts(body: SendMessage, docs: Any) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    if body.content:
        parts.append({"type": "text", "text": body.content})
    for a in body.attachments:
        if a.kind == "file":
            f = docs.get(a.file_id or "") if a.file_id and re.fullmatch(r"[a-f0-9]{32}", a.file_id) else None
            if not f:
                raise HTTPException(400, f"Datei {a.name} nicht gefunden – bitte erneut hochladen")
            parts.append(
                {
                    "type": "file",
                    "file_id": f["id"],
                    "name": f["name"],
                    "kind": f["kind"],
                    "pages": f.get("pages"),
                    "chars": f.get("chars"),
                    "status": f["status"],
                }
            )
        elif a.kind == "text":
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
    parts = _user_parts(body, get_services(request).documents)
    get_services(request).documents.attach_to_chat([p["file_id"] for p in parts if p.get("file_id")], chat_id)
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


# ------------------------------------------------- Eigene Anweisungen & Nachschlagewerke
MAX_REFERENCE_CHARS = 2_000_000


@router.get("/settings")
async def get_settings_(request: Request):
    s = get_services(request)
    return {"custom_instructions": s.store.get_custom_instructions()}


@router.put("/settings")
async def put_settings(request: Request, body: SettingsUpdate):
    s = get_services(request)
    s.store.set_custom_instructions(body.custom_instructions)
    return {"custom_instructions": s.store.get_custom_instructions()}


@router.get("/references")
async def list_references(request: Request):
    return get_services(request).store.list_reference_docs()


@router.post("/references", status_code=201)
async def create_reference(request: Request, body: ReferenceIn):
    if len(body.content) > MAX_REFERENCE_CHARS:
        raise HTTPException(400, "Dokument ist zu groß (max. 2 Mio. Zeichen)")
    store = get_services(request).store
    rid = store.create_reference_doc(body.title, body.description, body.content, body.mode, body.enabled)
    return store.get_reference_doc(rid)


@router.get("/references/{doc_id}")
async def get_reference(request: Request, doc_id: int):
    doc = get_services(request).store.get_reference_doc(doc_id)
    if not doc:
        raise HTTPException(404, "Nicht gefunden")
    return doc


@router.patch("/references/{doc_id}")
async def patch_reference(request: Request, doc_id: int, body: ReferencePatch):
    store = get_services(request).store
    if not store.get_reference_doc(doc_id):
        raise HTTPException(404, "Nicht gefunden")
    if body.content is not None and len(body.content) > MAX_REFERENCE_CHARS:
        raise HTTPException(400, "Dokument ist zu groß (max. 2 Mio. Zeichen)")
    return store.update_reference_doc(doc_id, **body.model_dump())


@router.delete("/references/{doc_id}")
async def delete_reference(request: Request, doc_id: int):
    if not get_services(request).store.delete_reference_doc(doc_id):
        raise HTTPException(404, "Nicht gefunden")
    return {"deleted": doc_id}


# ------------------------------------------------------- Modelle, Preise, Verbrauch
class ModelInfoUpdate(BaseModel):
    context_window: int | None = Field(default=None, ge=1)
    input_cost_per_mtok: float | None = Field(default=None, ge=0)
    output_cost_per_mtok: float | None = Field(default=None, ge=0)
    cached_cost_per_mtok: float | None = Field(default=None, ge=0)


def _model_rows(request: Request) -> list[dict[str, Any]]:
    s = get_services(request)
    names = list(dict.fromkeys([*s.settings.chat_model_list, s.settings.effective_background_model]))
    rows = []
    for name in names:
        info = s.catalog.info(name)
        role = "chat" if name in s.settings.chat_model_list else "hintergrund"
        if name == s.settings.effective_background_model and role == "chat":
            role = "chat + hintergrund"
        rows.append({"model": name, "role": role, **info})
    return rows


@router.get("/models")
async def models(request: Request, refresh: bool = False):
    """Kontextfenster und Preise je Modell (von LiteLLM, ggf. manuell überschrieben)."""
    await get_services(request).catalog.refresh(force=refresh)
    return _model_rows(request)


@router.put("/models/{model:path}")
async def set_model_info(request: Request, model: str, body: ModelInfoUpdate):
    s = get_services(request)
    s.catalog.set_overrides(model, body.model_dump())
    return {"model": model, **s.catalog.info(model)}


@router.get("/usage")
async def usage(request: Request, days: int = 30):
    """Verbrauch der letzten Tage – Chats und Hintergrundaufgaben (Dreaming, Titel …)."""
    return get_services(request).usage_log.summary(max(1, min(days, 365)))


# ------------------------------------------------------------- Export & Import

class ExportRequest(BaseModel):
    name: str = "Second-Brain-Paket"
    description: str = ""
    servers: list[str] = Field(default_factory=list)
    references: list[int] = Field(default_factory=list)
    tool_policies: bool = True
    custom_instructions: bool = False


class ImportRequest(BaseModel):
    bundle: dict[str, Any]
    decisions: dict[str, Any] = Field(default_factory=dict)


@router.get("/bundles/exportable")
async def bundle_exportable(request: Request):
    """Was sich exportieren lässt (MCP-Server, Nachschlagewerke, eigene Anweisungen)."""
    return bundles.exportable(get_services(request))


@router.post("/bundles/export")
async def bundle_export(request: Request, body: ExportRequest):
    if not body.servers and not body.references and not body.custom_instructions:
        raise HTTPException(400, "Nichts ausgewählt")
    try:
        return bundles.build(get_services(request), **body.model_dump())
    except (bundles.BundleError, bundles.ConfigError) as e:
        raise HTTPException(400, str(e)) from e


@router.post("/bundles/preview")
async def bundle_preview(request: Request, bundle: dict[str, Any]):
    try:
        return bundles.preview(get_services(request), bundle)
    except bundles.BundleError as e:
        raise HTTPException(400, str(e)) from e


@router.post("/bundles/import")
async def bundle_import(request: Request, body: ImportRequest):
    s = get_services(request)
    try:
        result = bundles.apply(s, body.bundle, body.decisions)
    except bundles.BundleError as e:
        raise HTTPException(400, str(e)) from e
    if result["servers"] or result["secrets"]:
        await s.mcp.reload()
        await s.mcp.wait_until_settled(20)
    return {"result": result, "mcp": s.mcp.status()}


# ------------------------------------------------------------------- Dateien
def _file_or_404(request: Request, file_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9]{32}", file_id):
        raise HTTPException(404, "Datei nicht gefunden")
    f = get_services(request).documents.get(file_id)
    if not f:
        raise HTTPException(404, "Datei nicht gefunden")
    return f


@router.post("/files", status_code=201)
async def upload_file(request: Request, file: UploadFile = File(...), chat_id: str | None = Form(default=None)):
    """Datei hochladen und lesen (PDF, Word, PowerPoint, Excel, Bilder, Text …)."""
    s = get_services(request)
    data = await file.read()
    if len(data) > s.settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"Datei ist größer als {s.settings.max_upload_mb} MB")
    if not data:
        raise HTTPException(400, "Leere Datei")
    return await asyncio.to_thread(s.documents.save_upload, file.filename or "datei", data, file.content_type, chat_id)


@router.get("/files/{file_id}")
async def file_info(request: Request, file_id: str):
    return _file_or_404(request, file_id)


@router.get("/files/{file_id}/content")
async def file_content(request: Request, file_id: str):
    """Was der Assistent aus der Datei gelesen hat (Markdown)."""
    f = _file_or_404(request, file_id)
    docs = get_services(request).documents
    return {**f, "content": docs.content(file_id), "sections": [s["label"] for s in docs.sections(file_id)]}


@router.get("/files/{file_id}/original")
async def file_original(request: Request, file_id: str):
    f = _file_or_404(request, file_id)
    path = get_services(request).documents.original_path(file_id)
    if not path:
        raise HTTPException(404, "Original nicht gefunden")
    return Response(
        path.read_bytes(),
        media_type=f["mime"] or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{f["name"]}"'},
    )


@router.get("/files/{file_id}/pages/{page}")
async def file_page(request: Request, file_id: str, page: int):
    _file_or_404(request, file_id)
    png = await asyncio.to_thread(get_services(request).documents.page_png, file_id, page)
    if not png:
        raise HTTPException(404, "Keine Seitenansicht verfügbar")
    return Response(png, media_type="image/png")

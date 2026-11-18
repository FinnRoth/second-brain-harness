"""OpenAI-kompatible Endpunkte für Open WebUI."""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..agent.session import MODES, PASSTHROUGH_PARAMS, ChatSession, parse_model
from ..llm import UpstreamError
from ..openwebui import is_task_request
from .deps import get_services, require_api_key

router = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])

MODE_LABELS = {"brain": "🧠", "incognito": "🕶️ Inkognito"}


@router.get("/models")
async def list_models(request: Request) -> dict[str, Any]:
    s = get_services(request)
    now = int(time.time())
    data = []
    for mode in MODES:
        for m in s.settings.chat_model_list:
            data.append(
                {
                    "id": f"{mode}/{m}",
                    "object": "model",
                    "created": now,
                    "owned_by": "second-brain-harness",
                    "name": f"{MODE_LABELS[mode]} {m}",
                }
            )
    return {"object": "list", "data": data}


async def _passthrough(request: Request, body: dict[str, Any]):
    """Open-WebUI-Hintergrundaufgaben (Titel, Tags …) ohne Gedächtnis durchreichen."""
    s = get_services(request)
    default = s.settings.chat_model_list[0] if s.settings.chat_model_list else "gpt-5"
    requested = body.get("model") or ""
    _, upstream = parse_model(requested, default)
    payload = {k: v for k, v in body.items() if k in PASSTHROUGH_PARAMS or k in ("messages", "tools", "tool_choice")}
    payload["model"] = upstream

    if body.get("stream"):

        async def gen():
            try:
                async for chunk in s.llm.chat_stream(payload):
                    chunk["model"] = requested
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            except UpstreamError as e:
                err = {"error": {"message": e.message, "code": e.status}}
                yield f"data: {json.dumps(err)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")
    try:
        resp = await s.llm.chat(payload)
    except UpstreamError as e:
        raise HTTPException(status_code=502, detail=f"LLM-API: {e.message}") from e
    resp["model"] = requested
    return JSONResponse(resp)


@router.post("/chat/completions")
async def chat_completions(request: Request):
    try:
        body = await request.json()
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail="Ungültiges JSON") from e
    if not isinstance(body, dict) or not body.get("messages"):
        raise HTTPException(status_code=400, detail="'messages' fehlt")

    if is_task_request(body):
        return await _passthrough(request, body)

    session = ChatSession(get_services(request), body, dict(request.headers))
    await session.prepare()

    if body.get("stream"):
        return StreamingResponse(
            session.stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    try:
        return JSONResponse(await session.complete())
    except UpstreamError as e:
        raise HTTPException(status_code=502, detail=f"LLM-API: {e.message}") from e

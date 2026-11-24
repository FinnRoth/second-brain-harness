"""Skills verwalten: anlegen, bearbeiten, (de)aktivieren, importieren, exportieren."""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from ..skills import MAX_ARCHIVE_BYTES, SkillError, is_text
from .deps import get_services, require_api_key

router = APIRouter(prefix="/api", tags=["skills"], dependencies=[Depends(require_api_key)])


class SkillBody(BaseModel):
    name: str
    description: str
    body: str = ""


class EnabledBody(BaseModel):
    enabled: bool


class SkillSettings(BaseModel):
    scripts_enabled: bool


def _skills(request: Request):
    return get_services(request).skills


@router.get("/skills")
async def list_skills(request: Request):
    store = _skills(request)
    return {"skills": store.list(), "scripts_enabled": store.scripts_enabled}


@router.put("/skills-settings")
async def skill_settings(request: Request, body: SkillSettings):
    _skills(request).set_scripts_enabled(body.scripts_enabled)
    return {"scripts_enabled": body.scripts_enabled}


@router.get("/skills/export")
async def export_skills(request: Request, names: str = Query("", description="kommagetrennt; leer = alle")):
    store = _skills(request)
    wanted = [n for n in names.split(",") if n] or [s["name"] for s in store.list()]
    if not wanted:
        raise HTTPException(404, "Keine Skills vorhanden")
    try:
        data = store.export_zip(wanted)
    except SkillError as e:
        raise HTTPException(404, str(e)) from e
    filename = f"{wanted[0]}.zip" if len(wanted) == 1 else "skills.zip"
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.post("/skills/import")
async def import_skills(
    request: Request,
    file: UploadFile = File(...),
    on_conflict: Literal["rename", "overwrite", "skip"] = Form("rename"),
):
    data = await file.read(MAX_ARCHIVE_BYTES + 1)
    if len(data) > MAX_ARCHIVE_BYTES:
        raise HTTPException(413, "Datei ist zu groß (max. 60 MB)")
    try:
        results = await asyncio.to_thread(_skills(request).import_archive, data, file.filename or "skill.zip", on_conflict)
    except SkillError as e:
        raise HTTPException(400, str(e)) from e
    return {"imported": results}


@router.post("/skills")
async def create_skill(request: Request, body: SkillBody):
    try:
        name = _skills(request).save(body.name, body.description, body.body)
    except SkillError as e:
        raise HTTPException(400, str(e)) from e
    return _skills(request).get(name)


@router.get("/skills/{name}")
async def get_skill(request: Request, name: str):
    skill = _skills(request).get(name)
    if not skill:
        raise HTTPException(404, "Skill nicht gefunden")
    return skill


@router.put("/skills/{name}")
async def update_skill(request: Request, name: str, body: SkillBody):
    try:
        new = _skills(request).save(body.name, body.description, body.body, old_name=name)
    except SkillError as e:
        raise HTTPException(400, str(e)) from e
    return _skills(request).get(new)


@router.put("/skills/{name}/enabled")
async def set_enabled(request: Request, name: str, body: EnabledBody):
    try:
        _skills(request).set_enabled(name, body.enabled)
    except SkillError as e:
        raise HTTPException(404, str(e)) from e
    return {"name": name, "enabled": body.enabled}


@router.delete("/skills/{name}")
async def delete_skill(request: Request, name: str):
    if not _skills(request).delete(name):
        raise HTTPException(404, "Skill nicht gefunden")
    return {"deleted": name}


@router.get("/skills/{name}/files/{path:path}")
async def skill_file(request: Request, name: str, path: str):
    try:
        p, data = _skills(request).read_file(name, path)
    except SkillError as e:
        raise HTTPException(404, str(e)) from e
    if is_text(p, data):
        return {"path": path, "text": data.decode("utf-8", errors="replace"), "size": len(data)}
    return {"path": path, "text": None, "size": len(data)}

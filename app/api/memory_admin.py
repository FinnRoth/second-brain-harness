"""REST-API zum Einsehen, Korrigieren und Pflegen des Gedächtnisses."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..memory.store import ENTITY_TYPES
from .deps import get_services, require_api_key

router = APIRouter(prefix="/memory", tags=["memory"], dependencies=[Depends(require_api_key)])


class EntityIn(BaseModel):
    type: str = Field(description=f"Einer von {', '.join(ENTITY_TYPES)}")
    name: str
    aliases: list[str] = []
    summary: str = ""
    attributes: dict[str, Any] = {}


class EntityPatch(BaseModel):
    name: str | None = None
    type: str | None = None
    aliases: list[str] | None = None
    summary: str | None = None
    attributes: dict[str, Any] | None = None
    status: str | None = None
    replace_attributes: bool = False


class FactIn(BaseModel):
    content: str
    entity_ids: list[int] = []
    category: str = "sonstiges"
    importance: int = 3


class FactPatch(BaseModel):
    content: str | None = None
    category: str | None = None
    importance: int | None = None
    entity_ids: list[int] | None = None
    supersede: bool = Field(
        default=True, description="Bei neuem Inhalt: alten Fakt als Historie behalten (true) oder überschreiben"
    )


class RelationIn(BaseModel):
    source_id: int
    target_id: int
    type: str
    description: str = ""


# --------------------------------------------------------------------- Profil
@router.get("/profile")
async def get_profile(request: Request):
    return get_services(request).store.get_profile()


@router.put("/profile")
async def set_profile(request: Request, body: dict[str, str]):
    s = get_services(request)
    s.store.set_profile(body.get("content", ""))
    return s.store.get_profile()


@router.post("/profile/rebuild")
async def rebuild_profile(request: Request):
    s = get_services(request)
    content = await s.profile.rebuild()
    return {"content": content}


@router.get("/summary")
async def get_summary(request: Request):
    """Memory-Zusammenfassung (Alias für /memory/profile)."""
    return get_services(request).store.get_profile()


@router.get("/prompt-preview")
async def prompt_preview(request: Request, message: str = "", conversation_id: str | None = None):
    """Zeigt, was der Assistent zu einer Nachricht sehen würde: Systemprompt + <kontext>-Block."""
    s = get_services(request)
    system, saved_ids = s.prompt_builder.system_prompt(conversation_id)
    ctx = await s.prompt_builder.turn_context([{"role": "user", "content": message}], saved_ids) if message else None
    return {"system": system, "context": ctx.text if ctx else None}


# ------------------------------------------------- Gespeicherte Erinnerungen
@router.get("/saved")
async def list_saved(request: Request):
    """Gespeicherte Erinnerungen – stehen in jedem Chat im Systemprompt."""
    return get_services(request).store.pinned_facts()


@router.post("/saved", status_code=201)
async def create_saved(request: Request, body: FactIn):
    s = get_services(request)
    fid = s.store.add_fact(body.content, body.entity_ids or [s.store.self_id()], body.category, body.importance, pinned=True)
    s.store.enqueue_job("index_embeddings", {"changes": 1}, dedupe_key="index")
    return s.store.get_fact(fid)


@router.post("/facts/{fact_id}/pin")
async def pin_fact(request: Request, fact_id: int):
    if not get_services(request).store.set_pinned(fact_id, True):
        raise HTTPException(404, "Nicht gefunden oder nicht aktiv")
    return {"pinned": fact_id}


@router.delete("/facts/{fact_id}/pin")
async def unpin_fact(request: Request, fact_id: int):
    if not get_services(request).store.set_pinned(fact_id, False):
        raise HTTPException(404, "Nicht gefunden oder nicht aktiv")
    return {"unpinned": fact_id}


# ----------------------------------------------------------------- Entitäten
@router.get("/entities")
async def list_entities(request: Request, type: str | None = None, status: str | None = None, q: str | None = None):
    s = get_services(request)
    counts = s.store.entity_fact_counts()
    return [{**e, "fact_count": counts.get(e["id"], 0)} for e in s.store.list_entities(type, status, q)]


@router.post("/entities", status_code=201)
async def create_entity(request: Request, body: EntityIn):
    s = get_services(request)
    if body.type not in ENTITY_TYPES or body.type == "self":
        raise HTTPException(400, f"Ungültiger Typ {body.type}")
    eid = s.store.create_entity(body.type, body.name, body.aliases, body.summary, body.attributes)
    return s.store.get_entity(eid)


@router.get("/entities/{entity_id}")
async def get_entity(request: Request, entity_id: int):
    s = get_services(request)
    ent = s.store.get_entity(entity_id)
    if not ent:
        raise HTTPException(404, "Nicht gefunden")
    return {
        **ent,
        "relations": s.store.relations_for_entity(entity_id),
        "facts": s.store.list_facts(entity_id=entity_id, include_inactive=True, limit=1000),
    }


@router.patch("/entities/{entity_id}")
async def patch_entity(request: Request, entity_id: int, body: EntityPatch):
    s = get_services(request)
    ent = s.store.update_entity(entity_id, **body.model_dump())
    if not ent:
        raise HTTPException(404, "Nicht gefunden")
    s.store.enqueue_job("index_embeddings", {"changes": 1}, dedupe_key="index")
    return ent


@router.delete("/entities/{entity_id}")
async def delete_entity(request: Request, entity_id: int):
    if not get_services(request).store.delete_entity(entity_id):
        raise HTTPException(404, "Nicht gefunden (oder Nutzer-Entität)")
    return {"deleted": entity_id}


@router.post("/entities/{entity_id}/consolidate")
async def consolidate_entity(request: Request, entity_id: int):
    """Räumt die Fakten einer Entität sofort auf (Dreaming manuell anstoßen)."""
    s = get_services(request)
    if not s.store.get_entity(entity_id):
        raise HTTPException(404, "Nicht gefunden")
    return await s.consolidator.consolidate(entity_id)


@router.post("/conversations/{conversation_id}/dream")
async def dream_conversation(request: Request, conversation_id: str):
    """Verarbeitet einen Chat sofort, statt auf die Inaktivitätszeit zu warten."""
    s = get_services(request)
    if not s.store.get_conversation(conversation_id):
        raise HTTPException(404, "Nicht gefunden")
    await s.worker.handlers["dream_conversation"](conversation_id=conversation_id)
    return {"ok": True}


@router.post("/entities/{keep_id}/merge/{merge_id}")
async def merge_entities(request: Request, keep_id: int, merge_id: int):
    """Führt ein Duplikat (merge_id) in keep_id zusammen."""
    s = get_services(request)
    ent = s.store.merge_entities(keep_id, merge_id)
    if not ent:
        raise HTTPException(400, "Zusammenführen nicht möglich")
    return ent


# ---------------------------------------------------------------------- Fakten
@router.get("/facts")
async def list_facts(
    request: Request,
    q: str | None = None,
    entity_id: int | None = None,
    include_inactive: bool = False,
    limit: int = 200,
):
    return get_services(request).store.list_facts(entity_id, q, include_inactive, limit=limit)


@router.get("/search")
async def search(request: Request, q: str, limit: int = 20):
    """Testet die Retrieval-Pipeline: Was würde der Assistent zu dieser Nachricht erinnern?"""
    s = get_services(request)
    ent_ids = s.retriever.detect_entities(q)
    hits = await s.retriever.search_facts(q, limit=limit, entity_ids=ent_ids)
    return {
        "detected_entities": [s.store.get_entity(i) for i in ent_ids],
        "facts": [{**f, "score": round(score, 5)} for f, score in hits],
    }


@router.post("/facts", status_code=201)
async def create_fact(request: Request, body: FactIn):
    s = get_services(request)
    ids = body.entity_ids or [s.store.self_id()]
    fid = s.store.add_fact(body.content, ids, body.category, body.importance)
    s.store.enqueue_job("index_embeddings", {"changes": 1}, dedupe_key="index")
    return s.store.get_fact(fid)


@router.patch("/facts/{fact_id}")
async def patch_fact(request: Request, fact_id: int, body: FactPatch):
    s = get_services(request)
    fact = s.store.get_fact(fact_id)
    if not fact:
        raise HTTPException(404, "Nicht gefunden")
    if body.content and body.supersede and body.content != fact["content"]:
        new_id = s.store.supersede_fact(
            fact_id, body.content, entity_ids=body.entity_ids, category=body.category, importance=body.importance
        )
        if not new_id:
            raise HTTPException(400, "Nur aktive Fakten können ersetzt werden")
        result = s.store.get_fact(new_id)
    else:
        result = s.store.edit_fact(fact_id, body.content, body.category, body.importance, body.entity_ids)
    s.store.enqueue_job("index_embeddings", {"changes": 1}, dedupe_key="index")
    return result


@router.delete("/facts/{fact_id}")
async def delete_fact(request: Request, fact_id: int, hard: bool = False):
    if not get_services(request).store.delete_fact(fact_id, hard=hard):
        raise HTTPException(404, "Nicht gefunden oder bereits inaktiv")
    return {"deleted": fact_id}


# ------------------------------------------------------------------ Relationen
@router.post("/relations", status_code=201)
async def create_relation(request: Request, body: RelationIn):
    s = get_services(request)
    if not s.store.get_entity(body.source_id) or not s.store.get_entity(body.target_id):
        raise HTTPException(404, "Entität nicht gefunden")
    rid = s.store.upsert_relation(body.source_id, body.target_id, body.type, body.description)
    return {"id": rid}


@router.delete("/relations/{relation_id}")
async def delete_relation(request: Request, relation_id: int):
    if not get_services(request).store.delete_relation(relation_id):
        raise HTTPException(404, "Nicht gefunden")
    return {"deleted": relation_id}


# ------------------------------------------------------------------- Gespräche
@router.get("/conversations")
async def list_conversations(request: Request, limit: int = 50):
    return get_services(request).store.list_conversations(limit)


@router.get("/conversations/{conversation_id}")
async def get_conversation(request: Request, conversation_id: str):
    s = get_services(request)
    conv = s.store.get_conversation(conversation_id)
    if not conv:
        raise HTTPException(404, "Nicht gefunden")
    return {**conv, "turns": s.store.turns_for_conversation(conversation_id)}


# ---------------------------------------------------------------- Wartung/Jobs
@router.get("/jobs")
async def list_jobs(request: Request, status: str | None = None, limit: int = 50):
    return get_services(request).store.list_jobs(status, limit)


@router.post("/jobs/run")
async def run_jobs(request: Request):
    """Arbeitet alle anstehenden Jobs sofort ab (auch zeitlich geplante)."""
    return {"processed": await get_services(request).worker.drain()}


@router.post("/export")
async def export_vault(request: Request):
    s = get_services(request)
    if not s.exporter.enabled:
        raise HTTPException(400, "EXPORT_DIR ist nicht gesetzt")
    return s.exporter.export()


@router.post("/reembed")
async def reembed(request: Request):
    """Alle Embeddings neu berechnen (z. B. nach Wechsel des Embedding-Modells)."""
    s = get_services(request)
    if not s.embedder.enabled:
        raise HTTPException(400, "EMBEDDING_MODEL ist nicht gesetzt")
    job_id = s.store.enqueue_job("reembed", dedupe_key="reembed")
    s.worker.notify()
    return {"job_id": job_id}


@router.get("/stats")
async def stats(request: Request):
    db = get_services(request).db
    return {
        "entities": {r["type"]: r["n"] for r in db.query("SELECT type, COUNT(*) n FROM entities GROUP BY type")},
        "facts": {r["status"]: r["n"] for r in db.query("SELECT status, COUNT(*) n FROM facts GROUP BY status")},
        "conversations": db.scalar("SELECT COUNT(*) FROM conversations"),
        "turns": db.scalar("SELECT COUNT(*) FROM turns"),
        "pending_jobs": db.scalar("SELECT COUNT(*) FROM jobs WHERE status='pending'"),
        "failed_jobs": db.scalar("SELECT COUNT(*) FROM jobs WHERE status='failed'"),
    }

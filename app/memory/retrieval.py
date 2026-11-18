"""Automatisches Erinnern: baut zu jeder Anfrage den relevanten Gedächtnis-Kontext.

Ablauf:
1. Entity-Detection (Namen/Aliase bekannter Personen, Projekte … im Text)
2. Hybrid-Suche: Volltext (FTS5/BM25) + optional Vektorähnlichkeit + Fakten der erkannten Entitäten
3. Fusion per Reciprocal Rank Fusion, gewichtet mit Wichtigkeit und Aktualität
4. Rendering in einen kompakten Markdown-Block innerhalb eines Token-Budgets
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..config import Settings
from .embeddings import Embedder, cosine_top_k
from .store import MemoryStore

TYPE_LABELS = {
    "self": "Nutzer",
    "person": "Person",
    "project": "Projekt",
    "organization": "Organisation",
    "idea": "Idee",
    "strategy": "Strategie",
    "goal": "Ziel",
    "life_area": "Lebensbereich",
    "topic": "Thema",
}


def approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _age_days(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400)


def text_of(content: Any) -> str:
    """Text aus einer OpenAI-Message-Content (String oder Liste von Parts)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") == "text":
                parts.append(p.get("text") or "")
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(parts)
    return str(content)


@dataclass
class RetrievedContext:
    text: str = ""
    fact_ids: list[int] = field(default_factory=list)
    entity_ids: list[int] = field(default_factory=list)


class Retriever:
    def __init__(self, store: MemoryStore, embedder: Embedder, settings: Settings) -> None:
        self.store = store
        self.embedder = embedder
        self.settings = settings

    # ------------------------------------------------------------ Entity-Detection
    def detect_entities(self, text: str, limit: int | None = None, include_self: bool = False) -> list[int]:
        if not text:
            return []
        hits: list[tuple[int, int]] = []  # (position, id)
        for eid, etype, names in self.store.entity_name_index():
            if etype == "self" and not include_self:
                continue
            best = None
            for name in names:
                name = name.strip()
                if len(name) < 3:
                    continue
                m = re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.IGNORECASE)
                if m and (best is None or m.start() < best):
                    best = m.start()
            if best is not None:
                hits.append((best, eid))
        hits.sort()
        ids = [eid for _, eid in hits]
        return ids[: limit or self.settings.max_detected_entities]

    async def similar_entities(self, text: str, k: int = 8, min_score: float = 0.35) -> list[int]:
        if not self.embedder.enabled:
            return []
        vec = await self.embedder.embed_one(text[:4000])
        if vec is None:
            return []
        return [i for i, _ in cosine_top_k(vec, self.store.entity_embeddings(), k, min_score)]

    # ---------------------------------------------------------------- Fakten-Suche
    async def search_facts(
        self,
        query: str,
        limit: int = 20,
        entity_ids: list[int] | None = None,
        query_vec: list[float] | None = None,
    ) -> list[tuple[dict[str, Any], float]]:
        ranked_lists: list[list[int]] = []

        fts = self.store.search_facts_fts(query, 40)
        if fts:
            ranked_lists.append([fid for fid, _ in fts])

        if self.embedder.enabled:
            if query_vec is None:
                query_vec = await self.embedder.embed_one(query[:4000])
            if query_vec is not None:
                vec_hits = cosine_top_k(query_vec, self.store.fact_embeddings(), 40, min_score=0.25)
                if vec_hits:
                    ranked_lists.append([fid for fid, _ in vec_hits])

        for eid in entity_ids or []:
            ent_facts = self.store.list_facts(entity_id=eid, order="importance", limit=15)
            if ent_facts:
                ranked_lists.append([f["id"] for f in ent_facts])

        rrf: dict[int, float] = {}
        for lst in ranked_lists:
            for rank, fid in enumerate(lst):
                rrf[fid] = rrf.get(fid, 0.0) + 1.0 / (60 + rank)
        if not rrf:
            return []

        facts = self.store.get_facts(rrf.keys())
        scored = []
        for f in facts:
            importance_w = 0.6 + 0.12 * f["importance"]  # 0.72 … 1.2
            recency_w = 0.6 + 0.4 * math.exp(-_age_days(f["updated_at"]) / 180)  # Halbwertszeit ~4 Monate
            scored.append((f, rrf[f["id"]] * importance_w * recency_w))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]

    # ------------------------------------------------------------ Kontext bauen
    async def build_context(self, messages: list[dict[str, Any]]) -> RetrievedContext:
        user_msgs = [text_of(m.get("content")) for m in messages if m.get("role") == "user"]
        if not user_msgs:
            return RetrievedContext()
        last_user = user_msgs[-1]
        prev_user = user_msgs[-2] if len(user_msgs) > 1 else ""
        last_assistant = ""
        for m in reversed(messages[:-1]):
            if m.get("role") == "assistant":
                last_assistant = text_of(m.get("content"))
                break

        # Entitäten: primär aus der aktuellen Nachricht, ergänzend aus dem direkten Kontext
        ent_ids = self.detect_entities(last_user)
        for eid in self.detect_entities(prev_user + "\n" + last_assistant[-1500:]):
            if eid not in ent_ids and len(ent_ids) < self.settings.max_detected_entities:
                ent_ids.append(eid)

        query = last_user[-3000:]
        if len(last_user) < 80 and prev_user:  # kurze Rückfragen ("und was meinst du?") brauchen Kontext
            query = prev_user[-1000:] + "\n" + last_user
        facts = await self.search_facts(query, limit=self.settings.max_context_facts, entity_ids=ent_ids[:4])

        text, used_facts, used_entities = self.render(ent_ids, facts)
        self.store.touch_facts(used_facts)
        return RetrievedContext(text=text, fact_ids=used_facts, entity_ids=used_entities)

    def entity_card(self, ent: dict[str, Any], max_relations: int = 8) -> str:
        label = TYPE_LABELS.get(ent["type"], ent["type"])
        line = f"- **{ent['name']}** ({label}"
        if ent["status"] != "active":
            line += f", {ent['status']}"
        line += ")"
        if ent["aliases"]:
            line += f" – auch: {', '.join(ent['aliases'][:5])}"
        if ent["summary"]:
            line += f": {ent['summary']}"
        if ent["attributes"]:
            attrs = "; ".join(f"{k}: {v}" for k, v in list(ent["attributes"].items())[:10])
            line += f" [{attrs}]"
        rels = []
        for r in self.store.relations_for_entity(ent["id"])[:max_relations]:
            if r["source_id"] == ent["id"]:
                rels.append(f"{r['type']} → {r['target_name']}")
            else:
                rels.append(f"{r['source_name']} {r['type']} → (diese)")
        if rels:
            line += "\n  Beziehungen: " + "; ".join(rels)
        return line

    def render(
        self, entity_ids: list[int], facts: list[tuple[dict[str, Any], float]]
    ) -> tuple[str, list[int], list[int]]:
        budget = self.settings.context_token_budget
        used = 0
        parts: list[str] = []
        used_entities: list[int] = []
        used_facts: list[int] = []

        cards = []
        for eid in entity_ids:
            ent = self.store.get_entity(eid)
            if not ent:
                continue
            card = self.entity_card(ent)
            cost = approx_tokens(card)
            if used + cost > budget * 0.45:  # max. ~45 % des Budgets für Steckbriefe
                break
            cards.append(card)
            used += cost
            used_entities.append(eid)
        if cards:
            parts.append("### Erwähnte Personen, Projekte & Themen\n" + "\n".join(cards))

        lines = []
        for f, _score in facts:
            date = (f["created_at"] or "")[:10]
            line = f"- {f['content']} _(#F{f['id']}, {date})_"
            cost = approx_tokens(line)
            if used + cost > budget:
                break
            lines.append(line)
            used += cost
            used_facts.append(f["id"])
        if lines:
            parts.append("### Relevante Erinnerungen\n" + "\n".join(lines))

        return "\n\n".join(parts), used_facts, used_entities

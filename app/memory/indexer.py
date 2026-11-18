"""Hält Embeddings aktuell: alles, was (noch) keinen Vektor hat, wird nachgezogen."""

from __future__ import annotations

from .embeddings import Embedder, to_blob
from .store import MemoryStore


def entity_embedding_text(ent: dict) -> str:
    parts = [ent["name"]]
    if ent["aliases"]:
        parts.append(f"({', '.join(ent['aliases'])})")
    if ent["summary"]:
        parts.append(f": {ent['summary']}")
    return " ".join(parts)


class Indexer:
    def __init__(self, store: MemoryStore, embedder: Embedder) -> None:
        self.store = store
        self.embedder = embedder

    async def index_pending(self) -> int:
        if not self.embedder.enabled:
            return 0
        count = 0
        while True:
            facts = self.store.facts_without_embedding(limit=256)
            if not facts:
                break
            vecs = await self.embedder.embed([f["content"] for f in facts])
            if vecs is None:
                return count
            for f, v in zip(facts, vecs):
                self.store.set_fact_embedding(f["id"], to_blob(v))
            count += len(facts)
        entities = self.store.entities_without_embedding()
        if entities:
            vecs = await self.embedder.embed([entity_embedding_text(e) for e in entities])
            if vecs is not None:
                for e, v in zip(entities, vecs):
                    self.store.set_entity_embedding(e["id"], to_blob(v))
                count += len(entities)
        return count

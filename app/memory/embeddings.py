"""Embeddings: optional, über LiteLLM. Vektorsuche per numpy (Brute-Force reicht
für ein persönliches Gedächtnis mit einigen zehntausend Einträgen locker)."""

from __future__ import annotations

import logging
from typing import Sequence

import numpy as np

from ..config import Settings
from ..llm import LLM

log = logging.getLogger(__name__)


def to_blob(vec: Sequence[float]) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def cosine_top_k(
    query: Sequence[float], items: list[tuple[int, bytes]], k: int, min_score: float = 0.0
) -> list[tuple[int, float]]:
    if not items:
        return []
    q = np.asarray(query, dtype=np.float32)
    qn = np.linalg.norm(q)
    if qn == 0:
        return []
    ids, vecs = [], []
    for item_id, blob in items:
        v = from_blob(blob)
        if v.shape[0] == q.shape[0]:
            ids.append(item_id)
            vecs.append(v)
    if not vecs:
        return []
    m = np.vstack(vecs)
    norms = np.linalg.norm(m, axis=1)
    norms[norms == 0] = 1.0
    scores = (m @ q) / (norms * qn)
    order = np.argsort(-scores)[:k]
    return [(ids[i], float(scores[i])) for i in order if scores[i] >= min_score]


class Embedder:
    def __init__(self, llm: LLM, settings: Settings) -> None:
        self.llm = llm
        self.model = settings.embedding_model.strip()

    @property
    def enabled(self) -> bool:
        return bool(self.model)

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        if not self.enabled or not texts:
            return None
        try:
            out: list[list[float]] = []
            for i in range(0, len(texts), 64):
                out.extend(await self.llm.embed(self.model, [t[:8000] for t in texts[i : i + 64]]))
            return out
        except Exception as e:  # Embeddings dürfen den Chat nie blockieren
            log.warning("Embedding fehlgeschlagen (%s) – Fallback auf Volltextsuche", e)
            return None

    async def embed_one(self, text: str) -> list[float] | None:
        res = await self.embed([text])
        return res[0] if res else None

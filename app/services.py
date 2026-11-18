"""Verdrahtung aller Komponenten."""

from __future__ import annotations

import logging

from .config import Settings
from .db import Database
from .jobs import Worker
from .llm import LLM, LiteLLMClient
from .memory.embeddings import Embedder
from .memory.export import VaultExporter
from .memory.extraction import Extractor
from .memory.indexer import Indexer
from .memory.profile import ConversationSummarizer, ProfileBuilder
from .memory.retrieval import Retriever
from .memory.store import MemoryStore

log = logging.getLogger(__name__)


class Services:
    def __init__(self, settings: Settings, llm: LLM | None = None) -> None:
        self.settings = settings
        self.db = Database(settings.db_path)
        self.store = MemoryStore(self.db)
        self.llm: LLM = llm or LiteLLMClient(settings)
        self.embedder = Embedder(self.llm, settings)
        self.retriever = Retriever(self.store, self.embedder, settings)
        self.extractor = Extractor(self.store, self.llm, self.retriever, settings)
        self.indexer = Indexer(self.store, self.embedder)
        self.profile = ProfileBuilder(self.store, self.llm, settings)
        self.summarizer = ConversationSummarizer(self.store, self.llm, self.embedder, settings)
        self.exporter = VaultExporter(self.store, settings.export_dir)
        self.worker = Worker(self)
        # Modelle, die Tools + reasoning_effort nicht zusammen akzeptieren (zur Laufzeit gelernt)
        self.tool_reasoning_conflicts: set[str] = set()

    async def startup(self) -> None:
        self.store.ensure_self(self.settings.user_name)
        self.store.reset_running_jobs()
        self.store.cleanup_jobs()
        if self.embedder.enabled:
            self.store.enqueue_job("index_embeddings", dedupe_key="index")
        if self.settings.enable_worker:
            self.worker.start()
        log.info(
            "Harness bereit – Modelle: %s, Hintergrundmodell: %s, Embeddings: %s",
            ", ".join(self.settings.chat_model_list),
            self.settings.effective_background_model,
            self.settings.embedding_model or "aus (nur Volltextsuche)",
        )

    async def shutdown(self) -> None:
        await self.worker.stop()
        await self.llm.close()
        self.db.close()

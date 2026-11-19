"""Verdrahtung aller Komponenten."""

from __future__ import annotations

import logging

from .config import Settings
from .db import Database
from .jobs import Worker
from .llm import LLM, LiteLLMClient
from .memory.consolidation import EntityConsolidator
from .memory.context import PromptBuilder
from .memory.embeddings import Embedder
from .memory.export import VaultExporter
from .memory.extraction import Extractor
from .memory.indexer import Indexer
from .memory.profile import ConversationSummarizer, ProfileBuilder
from .memory.retrieval import Retriever
from .mcp.manager import MCPManager
from .memory.store import MemoryStore
from .responses_api import ResponsesBackend
from .tools.mcp import MCPToolProvider
from .tools.memory import MemoryToolProvider
from .tools.registry import ToolRegistry

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
        self.consolidator = EntityConsolidator(self.store, self.llm, settings)
        self.prompt_builder = PromptBuilder(self.store, self.retriever, settings)
        self.summarizer = ConversationSummarizer(self.store, self.llm, self.embedder, settings)
        self.exporter = VaultExporter(self.store, settings.export_dir)
        self.worker = Worker(self)
        self.mcp = MCPManager(
            settings.mcp_config,
            settings.mcp_secrets,
            settings.mcp_log_dir,
            connect_timeout=settings.mcp_connect_timeout,
            watch_interval=settings.mcp_watch_interval,
        )
        self.tools = ToolRegistry(
            [MemoryToolProvider(self.store, self.retriever), MCPToolProvider(self.mcp, settings.mcp_max_output_chars)],
            search_threshold=settings.tool_search_threshold,
        )
        # Freigabe-Modi von Inkognito-Chats (werden nicht gespeichert)
        self.ephemeral_modes: dict[str, str] = {}
        self.responses = ResponsesBackend(self.llm, settings)
        # Zur Laufzeit gelernt:
        # - Modelle, die Tools + reasoning_effort auf /chat/completions nicht zusammen akzeptieren
        self.tool_reasoning_conflicts: set[str] = set()
        # - Modelle, die (automatisch) über /v1/responses laufen
        self.responses_models: set[str] = set(settings.responses_model_list)
        # - Modelle, bei denen /v1/responses nicht funktioniert hat
        self.responses_unsupported: set[str] = set()

    async def startup(self) -> None:
        self.store.ensure_self(self.settings.user_name)
        self.store.reset_running_jobs()
        self.store.cleanup_jobs()
        if self.embedder.enabled:
            self.store.enqueue_job("index_embeddings", dedupe_key="index")
        if self.settings.enable_worker:
            self.worker.start()
        await self.mcp.start(wait=self.settings.mcp_startup_wait)
        log.info(
            "Harness bereit – Modelle: %s, Hintergrundmodell: %s, Embeddings: %s",
            ", ".join(self.settings.chat_model_list),
            self.settings.effective_background_model,
            self.settings.embedding_model or "aus (nur Volltextsuche)",
        )

    def mcp_instructions(self) -> str:
        """Kurzbeschreibung der verbundenen MCP-Server für den Systemprompt (stabil → cachebar)."""
        lines = []
        for name, conn in sorted(self.mcp.connections.items()):
            if conn.status != "ready" or not conn.tools:
                continue
            line = f"- **{name}** ({len(conn.tools)} Tools)"
            if conn.instructions:
                line += ": " + " ".join(conn.instructions.split())[:400]
            lines.append(line)
        return "\n".join(lines)

    async def shutdown(self) -> None:
        await self.mcp.stop()
        await self.worker.stop()
        await self.llm.close()
        self.db.close()

"""Hintergrund-Worker: arbeitet die Job-Tabelle in SQLite ab.

Jobs überleben Neustarts; fehlgeschlagene Jobs werden mit Backoff wiederholt.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from .services import Services

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5


class Worker:
    def __init__(self, services: "Services") -> None:
        self.s = services
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._stopping = False
        self.handlers: dict[str, Callable[..., Awaitable[Any]]] = {
            "extract_turn": self._extract_turn,
            "rebuild_profile": self._rebuild_profile,
            "summarize_conversation": self._summarize,
            "index_embeddings": self._index,
            "export_vault": self._export,
            "reembed": self._reembed,
        }

    # ---------------------------------------------------------------- Steuerung
    def start(self) -> None:
        if self._task is None:
            self._stopping = False
            self._task = asyncio.create_task(self._loop(), name="memory-worker")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def notify(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        log.info("Hintergrund-Worker gestartet")
        while not self._stopping:
            try:
                ran = await self.run_one()
            except Exception:
                log.exception("Worker-Fehler")
                ran = False
            if not ran:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.s.settings.worker_poll_seconds)
                except asyncio.TimeoutError:
                    pass

    async def run_one(self, ignore_schedule: bool = False) -> bool:
        job = self.s.store.claim_next_job(ignore_schedule=ignore_schedule)
        if not job:
            return False
        handler = self.handlers.get(job["type"])
        if not handler:
            self.s.store.fail_job(job["id"], f"Unbekannter Job-Typ {job['type']}", None)
            return True
        try:
            await handler(**job["payload"])
            self.s.store.complete_job(job["id"])
        except Exception as e:
            retry = None if job["attempts"] >= MAX_ATTEMPTS else 30 * 2 ** (job["attempts"] - 1)
            log.warning("Job %s (%s) fehlgeschlagen (Versuch %d): %s", job["id"], job["type"], job["attempts"], e)
            self.s.store.fail_job(job["id"], repr(e), retry)
        return True

    async def drain(self, ignore_schedule: bool = True, max_jobs: int = 1000) -> int:
        """Arbeitet alle anstehenden Jobs sofort ab (für Tests und Admin)."""
        n = 0
        while n < max_jobs and await self.run_one(ignore_schedule=ignore_schedule):
            n += 1
        return n

    # ------------------------------------------------------------------- Jobs
    def _after_memory_change(self, changes: int) -> None:
        store, settings = self.s.store, self.s.settings
        if changes <= 0:
            return
        if store.bump_profile_changes(changes) >= settings.profile_rebuild_every or not store.get_profile()["content"]:
            store.enqueue_job("rebuild_profile", dedupe_key="profile")
        self._schedule_export()

    def _schedule_export(self) -> None:
        if self.s.exporter.enabled:
            self.s.store.enqueue_job("export_vault", delay_seconds=15, dedupe_key="export")

    async def _extract_turn(self, turn_id: int) -> None:
        result = await self.s.extractor.process_turn(turn_id)
        await self.s.indexer.index_pending()
        if result:
            self._after_memory_change(result.change_count)

    async def _rebuild_profile(self) -> None:
        await self.s.profile.rebuild()
        self._schedule_export()

    async def _summarize(self, conversation_id: str) -> None:
        await self.s.summarizer.summarize(conversation_id)
        self._schedule_export()

    async def _index(self, changes: int = 0) -> None:
        n = await self.s.indexer.index_pending()
        self._after_memory_change(changes)
        log.info("%d Embeddings aktualisiert", n)

    async def _export(self) -> None:
        stats = await asyncio.to_thread(self.s.exporter.export)
        log.info("Vault exportiert: %s", stats)

    async def _reembed(self) -> None:
        from .memory.embeddings import to_blob

        self.s.store.clear_embeddings()
        n = await self.s.indexer.index_pending()
        for c in self.s.store.list_conversations(limit=100000):
            if c.get("summary"):
                vec = await self.s.embedder.embed_one(f"{c['title']}: {c['summary']}")
                if vec is not None:
                    self.s.store.set_conversation_summary(c["id"], c["title"], c["summary"], to_blob(vec))
        log.info("Neu eingebettet: %d Einträge", n)

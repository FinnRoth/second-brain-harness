"""Kernprofil und Chat-Zusammenfassungen (Hintergrund-Jobs)."""

from __future__ import annotations

import json
import logging
from typing import Any

from .. import prompts
from ..config import Settings
from ..llm import LLM, message_content, parse_json_response
from .embeddings import Embedder, to_blob
from .extraction import today_str
from .retrieval import TYPE_LABELS
from .store import MemoryStore

log = logging.getLogger(__name__)


class ProfileBuilder:
    def __init__(self, store: MemoryStore, llm: LLM, settings: Settings, max_words: int = 450) -> None:
        self.store = store
        self.llm = llm
        self.settings = settings
        self.max_words = max_words

    def _material(self) -> str:
        out = [f"Heute: {today_str(self.settings)}"]
        self_id = self.store.self_id()
        me = self.store.get_entity(self_id)
        if me:
            out.append(f"## {me['name']} (Nutzer)\n{me['summary']}\n{json.dumps(me['attributes'], ensure_ascii=False)}")

        facts = self.store.list_facts(order="importance", min_importance=2, limit=300)
        lines = []
        for f in facts:
            ents = ", ".join(e["name"] for e in f["entities"])
            lines.append(f"- [imp {f['importance']}, {f['category']}, {f['created_at'][:10]}] {f['content']} ({ents})")
        out.append("## Fakten (nach Wichtigkeit)\n" + "\n".join(lines))

        counts = self.store.entity_fact_counts()
        for etype in ("project", "goal", "idea", "strategy", "organization", "life_area", "person"):
            ents = self.store.list_entities(type=etype, status="active", limit=200)
            ents.sort(key=lambda e: counts.get(e["id"], 0), reverse=True)
            ents = ents[: 20 if etype == "person" else 30]
            if not ents:
                continue
            block = []
            for e in ents:
                attrs = json.dumps(e["attributes"], ensure_ascii=False) if e["attributes"] else ""
                block.append(f"- {e['name']}: {e['summary']} {attrs} ({counts.get(e['id'], 0)} Fakten)")
            out.append(f"## {TYPE_LABELS.get(etype, etype)}\n" + "\n".join(block))
        return "\n\n".join(out)

    async def rebuild(self) -> str:
        if not self.store.list_facts(limit=1):
            return ""
        messages = [
            {
                "role": "system",
                "content": prompts.render("profile", user_name=self.settings.user_name, max_words=self.max_words),
            },
            {"role": "user", "content": self._material()},
        ]
        resp = await self.llm.chat({"model": self.settings.effective_background_model, "messages": messages})
        content = message_content(resp).strip()
        if content:
            self.store.set_profile(content)
            log.info("Profil neu erstellt (%d Zeichen)", len(content))
        return content


class ConversationSummarizer:
    def __init__(self, store: MemoryStore, llm: LLM, embedder: Embedder, settings: Settings) -> None:
        self.store = store
        self.llm = llm
        self.embedder = embedder
        self.settings = settings

    async def summarize(self, conversation_id: str) -> dict[str, Any] | None:
        turns = self.store.turns_for_conversation(conversation_id)
        if not turns:
            return None
        chunks = [f"USER: {t['user_message'][:3000]}\nASSISTENT: {t['assistant_message'][:2000]}" for t in turns]
        text = "\n\n".join(chunks)
        if len(text) > 24000:  # Anfang + Ende behalten
            text = text[:8000] + "\n\n[…]\n\n" + text[-16000:]
        payload: dict[str, Any] = {
            "model": self.settings.effective_background_model,
            "messages": [
                {"role": "system", "content": prompts.render("summary", user_name=self.settings.user_name)},
                {"role": "user", "content": text},
            ],
        }
        if self.settings.json_mode:
            payload["response_format"] = {"type": "json_object"}
        data = parse_json_response(message_content(await self.llm.chat(payload)))
        title = str(data.get("title") or "").strip()[:200] or "Ohne Titel"
        summary = str(data.get("summary") or "").strip()
        vec = await self.embedder.embed_one(f"{title}: {summary}") if summary else None
        self.store.set_conversation_summary(conversation_id, title, summary, to_blob(vec) if vec else None)
        return {"title": title, "summary": summary}

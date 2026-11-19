"""Dreaming, Teil 2: Wissen pro Entität aufräumen.

Nach der Verarbeitung von Chats werden Entitäten mit vielen (neuen) Fakten
konsolidiert: Duplikate zusammenführen, Überholtes entfernen, Steckbrief
neu schreiben. Abgelöste Fakten bleiben als Historie erhalten.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .. import prompts
from ..config import Settings
from ..llm import LLM, message_content, parse_json_response
from .extraction import today_str
from .store import MemoryStore

log = logging.getLogger(__name__)


def _fid(ref: Any) -> int | None:
    m = re.fullmatch(r"\s*[Ff]?(\d+)\s*", str(ref))
    return int(m.group(1)) if m else None


class EntityConsolidator:
    def __init__(self, store: MemoryStore, llm: LLM, settings: Settings) -> None:
        self.store = store
        self.llm = llm
        self.settings = settings

    def _meta_key(self, entity_id: int) -> str:
        return f"consolidated_count:{entity_id}"

    def needs_consolidation(self, entity_id: int) -> bool:
        count = len(self.store.list_facts(entity_id=entity_id, limit=10000))
        if count < self.settings.consolidation_min_facts:
            return False
        last = int(self.store.get_meta(self._meta_key(entity_id), "0") or 0)
        return count - last >= self.settings.consolidation_growth or last == 0

    async def consolidate(self, entity_id: int) -> dict[str, int]:
        ent = self.store.get_entity(entity_id)
        if not ent:
            return {}
        facts = self.store.list_facts(entity_id=entity_id, limit=300)
        if len(facts) < 2:
            return {}
        by_id = {f["id"]: f for f in facts}

        lines = [
            f"## Entität E{ent['id']} [{ent['type']}] {ent['name']}",
            f"Aliase: {', '.join(ent['aliases']) or '-'}",
            f"Zusammenfassung: {ent['summary'] or '-'}",
            f"Attribute: {json.dumps(ent['attributes'], ensure_ascii=False)}",
            "",
            "## Aktive Fakten",
        ]
        for f in sorted(facts, key=lambda f: f["created_at"]):
            pin = " [gemerkt]" if f["pinned"] else ""
            lines.append(f"- F{f['id']}{pin} ({f['created_at'][:10]}, imp {f['importance']}) {f['content']}")

        payload: dict[str, Any] = {
            "model": self.settings.effective_background_model,
            "messages": [
                {
                    "role": "system",
                    "content": prompts.render(
                        "consolidation", user_name=self.settings.user_name, today=today_str(self.settings)
                    ),
                },
                {"role": "user", "content": "\n".join(lines)},
            ],
        }
        if self.settings.json_mode:
            payload["response_format"] = {"type": "json_object"}
        data = parse_json_response(message_content(await self.llm.chat(payload)))

        stats = {"merged": 0, "deleted": 0}
        consumed: set[int] = set()
        with self.store.db.transaction():
            for m in data.get("merge") or []:
                if not isinstance(m, dict) or not (m.get("content") or "").strip():
                    continue
                ids = [i for i in (_fid(r) for r in m.get("ids") or []) if i in by_id and i not in consumed]
                if len(ids) < 2:
                    continue
                group = [by_id[i] for i in ids]
                entity_ids = {e["id"] for f in group for e in f["entities"]}
                try:
                    importance = int(m.get("importance") or max(f["importance"] for f in group))
                except (TypeError, ValueError):
                    importance = max(f["importance"] for f in group)
                new_id = self.store.add_fact(
                    m["content"],
                    entity_ids,
                    category=group[0]["category"],
                    importance=importance,
                    source_conversation_id=group[-1]["source_conversation_id"],
                    pinned=any(f["pinned"] for f in group),
                )
                for f in group:
                    self.store.db.execute(
                        "UPDATE facts SET status='superseded', superseded_by=?, updated_at=? WHERE id=?",
                        (new_id, f["updated_at"], f["id"]),
                    )
                consumed |= set(ids)
                stats["merged"] += len(ids)

            for ref in data.get("delete") or []:
                fid = _fid(ref)
                if fid in by_id and fid not in consumed and not by_id[fid]["pinned"]:
                    if self.store.delete_fact(fid):
                        stats["deleted"] += 1

            summary = (data.get("summary") or "").strip()
            attrs = data.get("attributes") if isinstance(data.get("attributes"), dict) else None
            if summary or attrs:
                self.store.update_entity(entity_id, summary=summary or None, attributes=attrs)

        remaining = len(self.store.list_facts(entity_id=entity_id, limit=10000))
        self.store.set_meta(self._meta_key(entity_id), str(remaining))
        log.info("Entität %s (%s) konsolidiert: %s", entity_id, ent["name"], stats)
        return stats

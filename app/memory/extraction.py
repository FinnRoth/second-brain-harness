"""Lernen nach jeder Antwort: Extraktion + Konsolidierung in einem LLM-Call.

Das LLM bekommt den Gesprächsabschnitt, die vermutlich betroffenen Entitäten
und bereits bekannte Fakten (mit IDs). Es antwortet mit Operationen
(add / supersede / delete), die hier validiert und angewendet werden.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .. import prompts
from ..config import Settings
from ..llm import LLM, message_content, parse_json_response
from .retrieval import TYPE_LABELS, Retriever
from .store import ENTITY_TYPES, MemoryStore

log = logging.getLogger(__name__)


@dataclass
class ExtractionResult:
    created_entities: list[int] = field(default_factory=list)
    updated_entities: list[int] = field(default_factory=list)
    added_facts: list[int] = field(default_factory=list)
    superseded_facts: list[tuple[int, int]] = field(default_factory=list)
    deleted_facts: list[int] = field(default_factory=list)
    relations: list[int] = field(default_factory=list)
    touched_entities: set[int] = field(default_factory=set)

    def merge(self, other: "ExtractionResult") -> None:
        self.created_entities += other.created_entities
        self.updated_entities += other.updated_entities
        self.added_facts += other.added_facts
        self.superseded_facts += other.superseded_facts
        self.deleted_facts += other.deleted_facts
        self.relations += other.relations
        self.touched_entities |= other.touched_entities

    @property
    def change_count(self) -> int:
        return (
            len(self.created_entities)
            + len(self.updated_entities)
            + len(self.added_facts)
            + len(self.superseded_facts)
            + len(self.deleted_facts)
        )


def _ref_id(ref: Any, prefix: str) -> int | None:
    if isinstance(ref, int):
        return ref
    m = re.fullmatch(rf"\s*{prefix}?(\d+)\s*", str(ref), re.IGNORECASE)
    return int(m.group(1)) if m else None


def today_str(settings: Settings) -> str:
    now = datetime.now(ZoneInfo(settings.timezone))
    weekdays = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
    return f"{weekdays[now.weekday()]}, {now.strftime('%d.%m.%Y')}"


class Extractor:
    def __init__(self, store: MemoryStore, llm: LLM, retriever: Retriever, settings: Settings) -> None:
        self.store = store
        self.llm = llm
        self.retriever = retriever
        self.settings = settings

    # ------------------------------------------------------------------ Ablauf
    async def process_conversation(self, conversation_id: str) -> ExtractionResult:
        """Dreaming: alle noch nicht verarbeiteten Turns eines Chats, in Blöcken."""
        total = ExtractionResult()
        conv = self.store.get_conversation(conversation_id)
        if not conv or conv["mode"] == "incognito":
            return total
        pending = self.store.unextracted_turns(conversation_id)
        budget = self.settings.extraction_max_chars
        batch: list[dict[str, Any]] = []
        size = 0
        for t in pending:
            t_size = len(t["user_message"]) + min(len(t["assistant_message"]), budget // 2)
            if batch and size + t_size > budget:
                total.merge(await self.process_turns(batch))
                batch, size = [], 0
            batch.append(t)
            size += t_size
        if batch:
            total.merge(await self.process_turns(batch))
        return total

    async def process_turn(self, turn_id: int) -> ExtractionResult | None:
        turn = self.store.get_turn(turn_id)
        if not turn:
            return None
        conv = self.store.get_conversation(turn["conversation_id"])
        if conv and conv["mode"] == "incognito":
            return None
        return await self.process_turns([turn])

    async def process_turns(self, turns: list[dict[str, Any]]) -> ExtractionResult:
        conversation_id = turns[0]["conversation_id"]
        max_chars = self.settings.extraction_max_chars
        user_text = "\n".join(t["user_message"] for t in turns)
        assistant_text = "\n".join(t["assistant_message"] for t in turns)

        # Kandidaten-Entitäten: im Text erkannt + semantisch ähnlich + der Nutzer selbst
        self_id = self.store.self_id()
        cand_entities = self.retriever.detect_entities(f"{user_text}\n{assistant_text}"[: max_chars * 2], limit=20)
        for eid in await self.retriever.similar_entities(user_text[-4000:], k=8):
            if eid not in cand_entities:
                cand_entities.append(eid)
        if self_id not in cand_entities:
            cand_entities.insert(0, self_id)

        # Kandidaten-Fakten: was zu diesem Abschnitt schon bekannt ist
        facts = await self.retriever.search_facts(
            user_text[-4000:] + "\n" + assistant_text[:1500], limit=50, entity_ids=cand_entities[:8]
        )
        prev_turns = self.store.turns_for_conversation(conversation_id, before_index=turns[0]["turn_index"], limit=2)

        messages = [
            {
                "role": "system",
                "content": prompts.render(
                    "extraction",
                    user_name=self.settings.user_name,
                    self_id=self_id,
                    today=today_str(self.settings),
                ),
            },
            {
                "role": "user",
                "content": self._build_input(cand_entities, [f for f, _ in facts], prev_turns, turns),
            },
        ]
        payload: dict[str, Any] = {"model": self.settings.effective_background_model, "messages": messages}
        if self.settings.json_mode:
            payload["response_format"] = {"type": "json_object"}
        resp = await self.llm.chat(payload)
        data = parse_json_response(message_content(resp))

        result = self.apply(data, conversation_id=conversation_id, turn_id=turns[-1]["id"])
        for t in turns:
            self.store.mark_turn_extracted(t["id"])
        log.info(
            "Chat %s (%d Turns): +%d Fakten, %d ersetzt, %d gelöscht, %d neue Entitäten",
            conversation_id,
            len(turns),
            len(result.added_facts),
            len(result.superseded_facts),
            len(result.deleted_facts),
            len(result.created_entities),
        )
        return result

    def _build_input(
        self,
        entity_ids: list[int],
        facts: list[dict[str, Any]],
        prev_turns: list[dict[str, Any]],
        turns: list[dict[str, Any]],
    ) -> str:
        max_chars = self.settings.extraction_max_chars
        out = [f"## Heute\n{today_str(self.settings)}"]

        ent_lines = []
        for eid in entity_ids:
            e = self.store.get_entity(eid)
            if not e:
                continue
            line = f"- E{e['id']} [{e['type']}] {e['name']}"
            if e["aliases"]:
                line += f" (Aliase: {', '.join(e['aliases'])})"
            if e["status"] != "active":
                line += f" ({e['status']})"
            if e["summary"]:
                line += f" – {e['summary']}"
            if e["attributes"]:
                line += f" {json.dumps(e['attributes'], ensure_ascii=False)}"
            ent_lines.append(line)
        out.append("## Bekannte Entitäten (Auswahl)\n" + ("\n".join(ent_lines) or "(keine)"))

        fact_lines = []
        for f in facts:
            refs = ", ".join(f"E{e['id']}" for e in f["entities"])
            pin = " [gemerkt]" if f.get("pinned") else ""
            fact_lines.append(f"- F{f['id']}{pin} [{refs}] ({f['created_at'][:10]}) {f['content']}")
        out.append("## Bestehende Fakten (Auswahl)\n" + ("\n".join(fact_lines) or "(keine)"))

        tool_writes = [
            t
            for turn in turns
            for t in turn.get("tool_log") or []
            if t.get("name") in {"remember", "forget_fact", "update_entity"}
        ]
        if tool_writes:
            lines = [f"- {t['name']}: {json.dumps(t.get('arguments'), ensure_ascii=False)}" for t in tool_writes]
            out.append(
                "## Bereits während des Gesprächs per Tool gespeichert (nicht erneut anlegen)\n" + "\n".join(lines)
            )

        if prev_turns:
            hist = []
            for t in prev_turns:
                hist.append(f"USER: {t['user_message'][:1500]}\nASSISTENT: {t['assistant_message'][:1000]}")
            out.append("## Vorheriger Verlauf (nur zum Verständnis)\n" + "\n\n".join(hist))

        section = []
        per_turn = max(2000, max_chars // max(1, len(turns)))
        for t in turns:
            section.append(f"USER: {t['user_message'][:per_turn]}\n\nASSISTENT: {t['assistant_message'][: per_turn // 2]}")
        out.append("## Zu analysierender Abschnitt\n" + "\n\n---\n\n".join(section))
        return "\n\n".join(out)

    # --------------------------------------------------------------- Anwenden
    def apply(self, data: dict[str, Any], conversation_id: str | None, turn_id: int | None) -> ExtractionResult:
        result = ExtractionResult()
        refs: dict[str, int] = {}
        self_id = self.store.self_id()

        def resolve(ref: Any) -> int | None:
            if ref is None:
                return None
            key = str(ref).strip()
            if key in refs:
                return refs[key]
            if re.fullmatch(r"[Ee]\d+", key):
                eid = int(key[1:])
                return eid if self.store.get_entity(eid) else None
            # Fallback: das Modell hat statt einer Referenz einen Namen geliefert
            ent = self.store.find_entity_by_name(key)
            return ent["id"] if ent else None

        with self.store.db.transaction():
            # 1) Entitäten
            for e in data.get("entities") or []:
                if not isinstance(e, dict):
                    continue
                ref = str(e.get("ref") or "").strip()
                name = (e.get("name") or "").strip()
                etype = (e.get("type") or "topic").strip().lower()
                if etype not in ENTITY_TYPES:
                    etype = "topic"
                aliases = [a for a in (e.get("aliases") or []) if isinstance(a, str)]
                attributes = e.get("attributes") if isinstance(e.get("attributes"), dict) else None
                status = e.get("status") if e.get("status") in ("active", "archived") else None

                existing_id = _ref_id(ref, "E") if re.fullmatch(r"[Ee]\d+", ref) else None
                if existing_id and self.store.get_entity(existing_id):
                    self.store.update_entity(
                        existing_id,
                        aliases=aliases,
                        summary=e.get("summary") if e.get("summary") else None,
                        attributes=attributes,
                        status=status,
                    )
                    refs[ref] = existing_id
                    result.updated_entities.append(existing_id)
                    continue
                if not name:
                    continue
                if etype == "self":
                    refs[ref or name] = self_id
                    continue
                # Duplikat-Schutz: gleicher Name (oder Alias) desselben Typs existiert schon
                dup = self.store.find_entity_by_name(name, etype)
                if not dup:
                    for alias in aliases:
                        dup = self.store.find_entity_by_name(alias, etype)
                        if dup:
                            break
                if dup:
                    self.store.update_entity(
                        dup["id"],
                        aliases=[name, *aliases],
                        summary=e.get("summary") or None,
                        attributes=attributes,
                        status=status,
                    )
                    refs[ref or name] = dup["id"]
                    result.updated_entities.append(dup["id"])
                    continue
                new_id = self.store.create_entity(
                    etype, name, aliases, e.get("summary") or "", attributes or {}, status or "active"
                )
                refs[ref or name] = new_id
                result.created_entities.append(new_id)

            # 2) Fakten
            for f in data.get("facts") or []:
                if not isinstance(f, dict):
                    continue
                op = (f.get("op") or "add").lower()
                content = (f.get("content") or "").strip()
                entity_ids = [eid for eid in (resolve(r) for r in f.get("entities") or []) if eid]
                importance = f.get("importance", 3)
                try:
                    importance = int(importance)
                except (TypeError, ValueError):
                    importance = 3
                category = f.get("category") or "sonstiges"
                target = _ref_id(f.get("target"), "F") if f.get("target") is not None else None
                target_fact = self.store.get_fact(target) if target else None
                target_active = target_fact is not None and target_fact["status"] == "active"

                if op == "delete":
                    if target_active and self.store.delete_fact(target):
                        result.deleted_facts.append(target)
                    continue
                if not content:
                    continue
                if op in ("supersede", "update") and target_active:
                    new_id = self.store.supersede_fact(
                        target,
                        content,
                        entity_ids=entity_ids,
                        category=category,
                        importance=importance,
                        source_conversation_id=conversation_id,
                        source_turn_id=turn_id,
                    )
                    if new_id:
                        result.superseded_facts.append((target, new_id))
                        result.touched_entities |= {e["id"] for e in target_fact["entities"]} | set(entity_ids)
                    continue
                if not entity_ids:
                    entity_ids = [self_id]
                dup = self.store.find_duplicate_fact(content)
                if dup:
                    self.store.link_fact_entities(dup, entity_ids)
                    continue
                fid = self.store.add_fact(
                    content,
                    entity_ids,
                    category=category,
                    importance=importance,
                    source_conversation_id=conversation_id,
                    source_turn_id=turn_id,
                )
                result.added_facts.append(fid)
                result.touched_entities |= set(entity_ids)

            # 3) Relationen
            for r in data.get("relations") or []:
                if not isinstance(r, dict):
                    continue
                s, t = resolve(r.get("source")), resolve(r.get("target"))
                if s and t and r.get("type"):
                    rid = self.store.upsert_relation(s, t, str(r["type"]), str(r.get("description") or ""))
                    if rid:
                        result.relations.append(rid)

        return result


def describe_entity_type(etype: str) -> str:
    return TYPE_LABELS.get(etype, etype)

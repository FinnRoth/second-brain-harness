"""Interne Gedächtnis-Tools (ToolProvider)."""

from __future__ import annotations

import json
import logging
from typing import Any

from ..memory.retrieval import TYPE_LABELS, Retriever
from ..memory.store import ENTITY_TYPES, MemoryStore
from .base import ToolContext, ToolResult, ToolSpec

log = logging.getLogger(__name__)

_ENTITY_TYPE_ENUM = [t for t in ENTITY_TYPES if t != "self"]


def _fn(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


TOOL_DEFS: dict[str, dict[str, Any]] = {
    "search_memory": _fn(
        "search_memory",
        "Durchsucht das Langzeitgedächtnis (Fakten über den Nutzer, Personen, Projekte, Ideen, Strategien …). "
        "Nutze es, wenn du mehr Kontext brauchst, als automatisch bereitgestellt wurde.",
        {
            "query": {"type": "string", "description": "Wonach gesucht wird, in natürlicher Sprache"},
            "limit": {"type": "integer", "description": "Max. Anzahl Ergebnisse (Standard 12)"},
        },
        ["query"],
    ),
    "get_entity": _fn(
        "get_entity",
        "Lädt den vollständigen Steckbrief einer Person, eines Projekts, einer Organisation, Idee, Strategie usw. "
        "inkl. aller bekannten Fakten und Beziehungen.",
        {"name": {"type": "string", "description": "Name, Alias oder ID (z. B. 'E12')"}},
        ["name"],
    ),
    "list_entities": _fn(
        "list_entities",
        "Listet bekannte Entitäten eines Typs auf, z. B. alle Projekte oder alle Personen.",
        {
            "type": {"type": "string", "enum": _ENTITY_TYPE_ENUM},
            "include_archived": {"type": "boolean", "description": "Auch archivierte/abgeschlossene anzeigen"},
        },
        ["type"],
    ),
    "search_past_chats": _fn(
        "search_past_chats",
        "Durchsucht frühere Gespräche (Zusammenfassungen und Wortlaut). Nutze es für Fragen wie "
        "'Was hatten wir zu X besprochen?'.",
        {"query": {"type": "string"}, "limit": {"type": "integer"}},
        ["query"],
    ),
    "remember": _fn(
        "remember",
        "Speichert sofort einen Fakt im Langzeitgedächtnis. Nur nutzen, wenn der Nutzer explizit darum bittet "
        "oder etwas besonders wichtig ist – alles andere wird automatisch gelernt.",
        {
            "content": {
                "type": "string",
                "description": "Eigenständig verständliche Aussage in der dritten Person, mit ausgeschriebenen Namen",
            },
            "entities": {
                "type": "array",
                "description": "Betroffene Entitäten (werden angelegt, falls unbekannt)",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "type": {"type": "string", "enum": _ENTITY_TYPE_ENUM},
                    },
                    "required": ["name"],
                },
            },
            "importance": {"type": "integer", "description": "1–5, Standard 4"},
            "category": {"type": "string"},
        },
        ["content"],
    ),
    "forget_fact": _fn(
        "forget_fact",
        "Markiert einen gespeicherten Fakt als gelöscht (z. B. bei 'Vergiss …' oder wenn er falsch ist). "
        "Optional mit korrigiertem Ersatz.",
        {
            "fact_id": {"type": "string", "description": "ID des Fakts, z. B. 'F42'"},
            "replacement": {"type": "string", "description": "Korrigierte Fassung (optional)"},
        },
        ["fact_id"],
    ),
    "update_entity": _fn(
        "update_entity",
        "Aktualisiert eine Entität: Zusammenfassung, Attribute (z. B. Projektstatus) oder archiviert sie.",
        {
            "name": {"type": "string", "description": "Name, Alias oder ID"},
            "summary": {"type": "string"},
            "attributes": {"type": "object", "description": "Zu setzende Attribute; leerer Wert entfernt"},
            "status": {"type": "string", "enum": ["active", "archived"]},
        },
        ["name"],
    ),
}

WRITE_TOOLS = {"remember", "forget_fact", "update_entity"}


def describe_call(name: str, args: dict[str, Any]) -> str:
    """Kurze, menschenlesbare Beschreibung für die Aktivitätsanzeige."""
    if name == "search_memory":
        return f"Gedächtnis durchsucht: „{args.get('query', '')}“"
    if name == "get_entity":
        return f"Steckbrief geladen: {args.get('name', '')}"
    if name == "list_entities":
        return f"Liste geladen: {TYPE_LABELS.get(args.get('type', ''), args.get('type', ''))}"
    if name == "search_past_chats":
        return f"Frühere Chats durchsucht: „{args.get('query', '')}“"
    if name == "remember":
        return f"Gemerkt: {args.get('content', '')}"
    if name == "forget_fact":
        return f"Vergessen: {args.get('fact_id', '')}"
    if name == "update_entity":
        return f"Aktualisiert: {args.get('name', '')}"
    return name


class MemoryToolProvider:
    """Stellt die Gedächtnis-Tools bereit. Lesende sind `read`, schreibende `memory`."""

    def __init__(self, store: MemoryStore, retriever: Retriever) -> None:
        self.store = store
        self.retriever = retriever

    def list_tools(self, ctx: ToolContext) -> list[ToolSpec]:
        specs = []
        for name, d in TOOL_DEFS.items():
            fn = d["function"]
            specs.append(
                ToolSpec(
                    name=name,
                    description=fn["description"],
                    parameters=fn["parameters"],
                    kind="memory" if name in WRITE_TOOLS else "read",
                    source="gedächtnis",
                )
            )
        return specs

    async def call(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            return ToolResult(f"Fehler: Tool '{name}' ist nicht verfügbar.", is_error=True)
        try:
            text = await handler(ctx, **args)
        except TypeError as e:
            return ToolResult(f"Fehler: ungültige Argumente ({e}).", is_error=True)
        except Exception as e:  # Tool-Fehler sollen die Antwort nie abbrechen
            log.exception("Tool %s fehlgeschlagen", name)
            return ToolResult(f"Fehler bei der Ausführung: {e}", is_error=True)
        if name in WRITE_TOOLS:
            ctx.memory_written = True
        return ToolResult(text)

    # ------------------------------------------------------------------ Lesen
    async def _t_search_memory(self, ctx: ToolContext, query: str, limit: int = 12, **_: Any) -> str:
        ent_ids = self.retriever.detect_entities(query)
        hits = await self.retriever.search_facts(query, limit=max(1, min(int(limit), 40)), entity_ids=ent_ids)
        lines = []
        for eid in ent_ids[:3]:
            ent = self.store.get_entity(eid)
            if ent:
                lines.append(self.retriever.entity_card(ent))
        for f, _ in hits:
            ents = ", ".join(e["name"] for e in f["entities"])
            lines.append(f"- F{f['id']} ({f['created_at'][:10]}; {ents}): {f['content']}")
        return "\n".join(lines) or "Keine passenden Erinnerungen gefunden."

    async def _t_get_entity(self, ctx: ToolContext, name: str, **_: Any) -> str:
        ent = self.store.resolve_entity(name)
        if not ent:
            candidates = self.store.list_entities(q=name, limit=5)
            if candidates:
                return "Nicht eindeutig gefunden. Meintest du: " + ", ".join(
                    f"{c['name']} (E{c['id']}, {c['type']})" for c in candidates
                )
            return f"Keine Entität '{name}' bekannt."
        out = [f"E{ent['id']} " + self.retriever.entity_card(ent, max_relations=30)]
        facts = self.store.list_facts(entity_id=ent["id"], limit=80)
        if facts:
            out.append("Fakten:")
            out += [f"- F{f['id']} ({f['created_at'][:10]}): {f['content']}" for f in facts]
        return "\n".join(out)

    async def _t_list_entities(self, ctx: ToolContext, type: str, include_archived: bool = False, **_: Any) -> str:
        ents = self.store.list_entities(type=type, status=None if include_archived else "active", limit=200)
        if not ents:
            return "Keine Einträge."
        lines = []
        for e in ents:
            line = f"- {e['name']} (E{e['id']})"
            if e["status"] != "active":
                line += f" [{e['status']}]"
            if e["attributes"]:
                line += " " + json.dumps(e["attributes"], ensure_ascii=False)
            if e["summary"]:
                line += f": {e['summary'][:200]}"
            lines.append(line)
        return "\n".join(lines)

    async def _t_search_past_chats(self, ctx: ToolContext, query: str, limit: int = 6, **_: Any) -> str:
        limit = max(1, min(int(limit), 20))
        out = []
        seen_convs = set()
        for c in self.store.search_conversation_summaries(query, limit=limit):
            seen_convs.add(c["id"])
            out.append(f"- Chat „{c['title']}“ ({c['updated_at'][:10]}): {c['summary']}")
        for t in self.store.search_turns_fts(query, limit=limit):
            title = t.get("conversation_title") or t["conversation_id"]
            out.append(
                f"- Aus „{title}“ ({t['created_at'][:10]}):\n  USER: {t['user_message'][:500]}\n"
                f"  ASSISTENT: {t['assistant_message'][:700]}"
            )
        return "\n".join(out) or "Keine passenden früheren Gespräche gefunden."

    # --------------------------------------------------------------- Schreiben
    async def _t_remember(self, ctx: ToolContext,
        content: str,
        entities: list[dict[str, Any]] | None = None,
        importance: int = 4,
        category: str = "sonstiges",
        **_: Any,
    ) -> str:
        ids = []
        for e in entities or []:
            if isinstance(e, str):
                e = {"name": e}
            name = (e.get("name") or "").strip()
            if not name:
                continue
            ent = self.store.resolve_entity(name)
            if not ent:
                eid = self.store.create_entity(e.get("type") or "topic", name)
            else:
                eid = ent["id"]
            ids.append(eid)
        if not ids:
            ids = [self.store.self_id()]
        fid = self.store.add_fact(
            content,
            ids,
            category=category,
            importance=importance,
            source_conversation_id=ctx.conversation_id,
            pinned=True,  # = „Gespeicherte Erinnerung“, steht ab jetzt in jedem Chat
        )
        return f"Gespeichert als F{fid}."

    async def _t_forget_fact(self, ctx: ToolContext, fact_id: str | int, replacement: str | None = None, **_: Any) -> str:
        fid = int(str(fact_id).strip().lstrip("Ff"))
        fact = self.store.get_fact(fid)
        if not fact or fact["status"] != "active":
            return f"F{fid} existiert nicht oder ist nicht aktiv."
        if replacement:
            new_id = self.store.supersede_fact(fid, replacement, source_conversation_id=ctx.conversation_id)
            return f"F{fid} ersetzt durch F{new_id}."
        self.store.delete_fact(fid)
        return f"F{fid} gelöscht."

    async def _t_update_entity(self, ctx: ToolContext,
        name: str,
        summary: str | None = None,
        attributes: dict[str, Any] | None = None,
        status: str | None = None,
        **_: Any,
    ) -> str:
        ent = self.store.resolve_entity(name)
        if not ent:
            return f"Keine Entität '{name}' bekannt."
        self.store.update_entity(ent["id"], summary=summary, attributes=attributes, status=status)
        return f"E{ent['id']} ({ent['name']}) aktualisiert."

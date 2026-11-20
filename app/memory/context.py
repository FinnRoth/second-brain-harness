"""Prompt-Aufbau im ChatGPT-Stil.

Stabiler Teil (Systemprompt, ändert sich nur, wenn sich das Gedächtnis ändert –
damit greift das Prompt-Caching des Anbieters):
  Persona + Datum · Memory-Zusammenfassung · gespeicherte Erinnerungen · letzte Gespräche

Dynamischer Teil (pro Nachricht, direkt vor der aktuellen Nutzernachricht):
  <kontext> mit Uhrzeit, Steckbriefen erwähnter Entitäten und relevanten Fakten
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .. import prompts
from ..config import Settings
from .extraction import today_str
from .retrieval import Retriever, approx_tokens, text_of
from .store import MemoryStore

NO_SUMMARY = "_Noch keine Zusammenfassung vorhanden – sie entsteht mit der Zeit aus euren Gesprächen._"


def _fmt_date(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%d.%m.%Y")
    except ValueError:
        return iso[:10]


def _relative(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - dt).days
    if days <= 0:
        return "heute"
    if days == 1:
        return "gestern"
    if days < 30:
        return f"vor {days} Tagen"
    return f"am {dt.strftime('%d.%m.%Y')}"


@dataclass
class TurnContext:
    text: str = ""
    fact_ids: list[int] = field(default_factory=list)
    entity_ids: list[int] = field(default_factory=list)


class PromptBuilder:
    def __init__(self, store: MemoryStore, retriever: Retriever, settings: Settings) -> None:
        self.store = store
        self.retriever = retriever
        self.settings = settings

    # ------------------------------------------------------------ stabiler Teil
    def saved_memories_block(self) -> tuple[str, set[int]]:
        facts = self.store.pinned_facts()
        if not facts:
            return "_Keine._", set()
        budget = self.settings.saved_memories_token_budget
        lines, used, shown = [], 0, set()
        for f in facts:
            line = f"- {f['content']} _(#F{f['id']}, {_fmt_date(f['created_at'])})_"
            cost = approx_tokens(line)
            if used + cost > budget:
                break
            lines.append(line)
            used += cost
            shown.add(f["id"])
        hidden = len(facts) - len(shown)
        if hidden:
            lines.append(f"- _(+{hidden} weitere im Hintergrund – per `search_memory` abrufbar)_")
        return "\n".join(lines), shown

    def recent_chats_block(self, exclude_id: str | None) -> str:
        chats = self.store.recent_conversations(exclude_id, limit=self.settings.recent_chats_count)
        if not chats:
            return "_Noch keine._"
        budget = self.settings.recent_chats_token_budget
        lines, used = [], 0
        for c in chats:
            title = c.get("title") or (c["user_messages"][0][:60] if c["user_messages"] else "Ohne Titel")
            msgs = " ‖ ".join(m.strip().replace("\n", " ")[:160] for m in c["user_messages"] if m.strip())
            line = f"- {_fmt_date(c['updated_at'])} „{title}“: {msgs}"
            cost = approx_tokens(line)
            if used + cost > budget:
                break
            lines.append(line)
            used += cost
        return "\n".join(lines)

    def system_prompt(
        self, conversation_id: str | None, mode_hints: list[str] | None = None, tools_section: str = ""
    ) -> tuple[str, set[int]]:
        summary = self.store.get_profile().get("content") or NO_SUMMARY
        saved, saved_ids = self.saved_memories_block()
        last = self.store.last_activity(exclude_id=conversation_id)
        last_txt = f" Euer letztes Gespräch davor war {_relative(last)}." if last else ""
        hints = "".join(f"- {h}\n" for h in (mode_hints or []))
        text = prompts.render(
            "system",
            user_name=self.settings.user_name,
            today=today_str(self.settings),
            last_activity=last_txt,
            mode_hints=hints,
            summary=summary,
            saved_memories=saved,
            recent_chats=self.recent_chats_block(conversation_id),
            tools_section=f"\n## Verbundene Tools (MCP)\n{tools_section}\n" if tools_section else "",
            custom_instructions=self.custom_instructions_block(),
            references=self.references_block(),
        )
        return text, saved_ids

    def custom_instructions_block(self) -> str:
        text = self.store.get_custom_instructions()
        if not text:
            return ""
        user = self.settings.user_name
        return (
            f"\n## Persönliche Anweisungen von {user}\n"
            f"Diese Anweisungen hat {user} selbst in den Einstellungen festgelegt. Befolge sie.\n\n{text}\n"
        )

    def references_block(self) -> str:
        docs = self.store.list_reference_docs(with_content=True, enabled_only=True)
        if not docs:
            return ""
        always = [d for d in docs if d["mode"] == "always"]
        on_demand = [d for d in docs if d["mode"] != "always"]
        out = ["\n## Nachschlagewerke", f"Von {self.settings.user_name} hinterlegte Referenzen. Nutze sie, wenn sie zur Frage passen."]
        for d in always:
            out.append(f"\n### {d['title']}")
            if d["description"]:
                out.append(f"_{d['description']}_")
            out.append(d["content"].strip())
        if on_demand:
            out.append(
                "\nWeitere Nachschlagewerke – bei Bedarf mit `read_reference` laden "
                "(mit `query` nur die passenden Stellen, z. B. ein Stichwort):"
            )
            for d in on_demand:
                desc = f": {d['description']}" if d["description"] else ""
                out.append(f"- **{d['title']}** (R{d['id']}, ~{max(1, d['chars'] // 4)} Tokens){desc}")
        return "\n".join(out) + "\n"

    # ---------------------------------------------------------- dynamischer Teil
    async def turn_context(self, messages: list[dict[str, Any]], exclude_fact_ids: set[int]) -> TurnContext:
        ctx = await self.retriever.build_context(messages, exclude_fact_ids=exclude_fact_ids)
        now = datetime.now(ZoneInfo(self.settings.timezone))
        parts = [f"Aktuelle Zeit: {today_str(self.settings)}, {now:%H:%M} Uhr"]
        if ctx.text:
            parts.append(ctx.text)
        return TurnContext(text="\n\n".join(parts), fact_ids=ctx.fact_ids, entity_ids=ctx.entity_ids)

    @staticmethod
    def inject(messages: list[dict[str, Any]], context: str) -> list[dict[str, Any]]:
        """Setzt den Kontext-Block vor die letzte Nutzernachricht (ohne das Original zu verändern)."""
        if not context:
            return messages
        idx = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=None)
        if idx is None:
            return messages
        block = (
            '<kontext quelle="gedächtnis" hinweis="automatisch eingefügt, nicht vom Nutzer geschrieben">\n'
            f"{context}\n</kontext>\n\n"
        )
        msg = dict(messages[idx])
        content = msg.get("content")
        if isinstance(content, list):
            msg["content"] = [{"type": "text", "text": block}, *content]
        else:
            msg["content"] = block + text_of(content)
        out = list(messages)
        out[idx] = msg
        return out

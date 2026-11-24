"""Tool zum gezielten Lesen hochgeladener Dokumente (für große Dateien)."""

from __future__ import annotations

import re
from typing import Any

from ..documents import DocumentStore
from .base import ToolContext, ToolResult, ToolSpec
from .references import search_lines

MAX_CHARS = 30_000


def _parse_pages(spec: str, total: int) -> list[int]:
    pages: list[int] = []
    for part in re.split(r"[,;\s]+", str(spec).strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            pages.extend(range(min(a, b), max(a, b) + 1))
        elif part.isdigit():
            pages.append(int(part))
    return [p for p in dict.fromkeys(pages) if 1 <= p <= total][:50]


class DocumentToolProvider:
    def __init__(self, store: DocumentStore) -> None:
        self.store = store

    def list_tools(self, ctx: ToolContext) -> list[ToolSpec]:
        if not self.store.for_chat(ctx.conversation_id):
            return []
        return [
            ToolSpec(
                name="read_document",
                description=(
                    "Liest ein in diesem Chat hochgeladenes Dokument gezielt: mit `query` die passenden Stellen "
                    "(Stichwörter), mit `pages` bestimmte Seiten/Folien/Tabellenblätter (z. B. '3-5'). Ohne beides: "
                    "Gliederung und Umfang. Für große Dokumente, die nicht vollständig im Kontext stehen."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Dateiname (oder Teil davon) bzw. ID"},
                        "query": {"type": "string", "description": "Stichwörter für die Suche"},
                        "pages": {"type": "string", "description": "Seiten/Folien/Blätter, z. B. '2' oder '4-6'"},
                    },
                    "required": ["name"],
                },
                kind="read",
                source="dokumente",
                title="Dokument",
            )
        ]

    def _find(self, ctx: ToolContext, name: str) -> dict[str, Any] | None:
        files = self.store.for_chat(ctx.conversation_id)
        n = (name or "").strip().lower()
        for f in files:
            if f["id"].startswith(n) or f["name"].lower() == n:
                return f
        hits = [f for f in files if n and n in f["name"].lower()]
        return hits[-1] if hits else (files[-1] if len(files) == 1 else None)

    async def call(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        f = self._find(ctx, str(args.get("name", "")))
        if not f:
            names = ", ".join(x["name"] for x in self.store.for_chat(ctx.conversation_id))
            return ToolResult(f"Dokument nicht gefunden. In diesem Chat: {names or '–'}", is_error=True)
        sections = self.store.sections(f["id"])
        content = self.store.content(f["id"])
        query = (args.get("query") or "").strip()
        pages = (args.get("pages") or "").strip()
        if pages and sections:
            wanted = _parse_pages(pages, len(sections))
            if not wanted:
                return ToolResult(f"Ungültige Angabe. Verfügbar: 1–{len(sections)} ({sections[0]['label']} …)", is_error=True)
            text = "\n\n".join(f"## {sections[p - 1]['label']}\n{sections[p - 1]['text']}" for p in wanted)
            if len(text) > MAX_CHARS:
                text = text[:MAX_CHARS] + "\n[… gekürzt – weniger Seiten auf einmal anfordern]"
            return ToolResult(f"# {f['name']}\n{text}")
        if query:
            hits = search_lines(content, query)
            if hits:
                return ToolResult(f"# {f['name']} – Treffer für „{query}“ (Zeilennummern)\n{hits[:MAX_CHARS]}")
            return ToolResult(f"Keine Treffer für „{query}“ in {f['name']}. Andere Begriffe oder `pages` versuchen.")
        labels = ", ".join(s["label"] for s in sections[:80]) or "–"
        heads = [l for l in content.splitlines() if l.lstrip().startswith("#")][:80]
        return ToolResult(
            f"# {f['name']} ({f['kind']}, ~{len(content) // 4} Tokens)\nAbschnitte: {labels}\n"
            + ("Gliederung:\n" + "\n".join(heads) if heads else "")
        )

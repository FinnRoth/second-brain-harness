"""Nachschlagewerke (in der App hinterlegte Dokumente) als Tool."""

from __future__ import annotations

import re
from typing import Any

from ..memory.store import MemoryStore
from .base import ToolContext, ToolResult, ToolSpec

MAX_FULL_CHARS = 40_000
MAX_HITS = 40
CONTEXT_LINES = 2


def search_lines(content: str, query: str) -> str | None:
    """Zeilen, die zur Anfrage passen (mit etwas Umgebung), nach Trefferzahl sortiert."""
    words = [w for w in re.findall(r"\w{2,}", query.lower())]
    if not words:
        return None
    lines = content.splitlines()
    scored = []
    for i, line in enumerate(lines):
        low = line.lower()
        score = sum(1 for w in words if w in low)
        if score:
            scored.append((score, i))
    if not scored:
        return None
    # Zeilen mit den meisten passenden Suchwörtern zuerst, dann in Dokumentreihenfolge ausgeben
    picked = sorted(i for _, i in sorted(scored, key=lambda x: (-x[0], x[1]))[:MAX_HITS])
    keep: set[int] = set()
    for i in picked:
        keep.update(range(max(0, i - CONTEXT_LINES), min(len(lines), i + CONTEXT_LINES + 1)))
    out, prev = [], None
    for i in sorted(keep):
        if prev is not None and i != prev + 1:
            out.append("…")
        out.append(f"{i + 1}: {lines[i]}")
        prev = i
    return "\n".join(out)


class ReferenceToolProvider:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def list_tools(self, ctx: ToolContext) -> list[ToolSpec]:
        docs = self.store.list_reference_docs(enabled_only=True)
        if not any(d["mode"] != "always" for d in docs):
            return []
        names = ", ".join(f"„{d['title']}“" for d in docs if d["mode"] != "always")
        return [
            ToolSpec(
                name="read_reference",
                description=(
                    "Liest ein vom Nutzer hinterlegtes Nachschlagewerk (z. B. Link-Verzeichnisse, interne Übersichten). "
                    f"Verfügbar: {names}. Mit `query` werden nur die passenden Zeilen geliefert – "
                    "bei großen Dokumenten immer zuerst gezielt suchen."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Titel oder ID (z. B. 'R2') des Nachschlagewerks"},
                        "query": {"type": "string", "description": "Optional: Stichwörter, nach denen gesucht wird"},
                    },
                    "required": ["name"],
                },
                kind="read",
                source="nachschlagewerke",
                title="Nachschlagewerk",
            )
        ]

    async def call(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        doc = self.store.find_reference_doc(args.get("name", ""))
        if not doc:
            available = ", ".join(f"{d['title']} (R{d['id']})" for d in self.store.list_reference_docs(enabled_only=True))
            return ToolResult(f"Nachschlagewerk nicht gefunden. Verfügbar: {available or '–'}", is_error=True)
        content = doc["content"]
        query = (args.get("query") or "").strip()
        if query:
            hits = search_lines(content, query)
            if hits:
                return ToolResult(f"# {doc['title']} – Treffer für „{query}“ (Zeilennummern)\n{hits}")
            headings = [l for l in content.splitlines() if l.lstrip().startswith("#")][:40]
            hint = ("\nGliederung:\n" + "\n".join(headings)) if headings else ""
            return ToolResult(f"Keine Treffer für „{query}“ in „{doc['title']}“.{hint}")
        if len(content) > MAX_FULL_CHARS:
            return ToolResult(
                f"# {doc['title']}\n{content[:MAX_FULL_CHARS]}\n\n[… gekürzt – das Dokument hat {len(content)} Zeichen. "
                "Nutze `query`, um gezielt zu suchen.]"
            )
        return ToolResult(f"# {doc['title']}\n{content}")

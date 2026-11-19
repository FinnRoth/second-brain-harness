"""ToolRegistry: sammelt Tools aller Provider, wendet Freigabe-Modi an, Tool-Suche.

Sind sehr viele Tools aktiv (z. B. mehrere große MCP-Server), bekommt das Modell
statt aller Schemas nur `tool_search` (wie die Tool-Suche in Claude Code). Gefundene
Tools werden für den Rest der Antwort freigeschaltet.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .base import ToolContext, ToolProvider, ToolResult, ToolSpec
from .permissions import Decision, decide

TOOL_SEARCH = "tool_search"


@dataclass
class ResolvedTool:
    spec: ToolSpec
    provider: ToolProvider | None
    decision: Decision


@dataclass
class ToolSet:
    tools: dict[str, ResolvedTool]
    deferred: dict[str, ResolvedTool]


class ToolRegistry:
    def __init__(self, providers: list[ToolProvider], search_threshold: int = 40) -> None:
        self.providers = providers
        self.search_threshold = search_threshold

    def _all(self, ctx: ToolContext) -> list[tuple[ToolSpec, ToolProvider]]:
        out = []
        seen: set[str] = set()
        for p in self.providers:
            for spec in p.list_tools(ctx):
                if spec.name not in seen:
                    seen.add(spec.name)
                    out.append((spec, p))
        return out

    def resolve(self, ctx: ToolContext) -> ToolSet:
        """Alle Tools, die das Modell in diesem Kontext sehen darf (inkl. Entscheidung)."""
        visible: dict[str, ResolvedTool] = {}
        for spec, provider in self._all(ctx):
            d = decide(spec, ctx)
            if d != "hidden":
                visible[spec.name] = ResolvedTool(spec, provider, d)

        deferrable = [n for n, r in visible.items() if r.spec.source.startswith("mcp:")]
        if len(visible) <= self.search_threshold or not deferrable:
            return ToolSet(visible, {})
        # Tool-Suche: MCP-Tools nur nach Freischaltung anbieten
        out = {n: r for n, r in visible.items() if n not in deferrable or n in ctx.activated}
        out[TOOL_SEARCH] = ResolvedTool(
            ToolSpec(
                name=TOOL_SEARCH,
                description=(
                    f"Sucht unter {len(deferrable)} weiteren Tools (MCP-Server wie Dateien, Kalender, GitHub …) "
                    "und schaltet passende frei. Nutze es, wenn du für die Aufgabe ein externes Tool brauchst."
                ),
                parameters={
                    "type": "object",
                    "properties": {"query": {"type": "string", "description": "Wofür du ein Tool suchst"}},
                    "required": ["query"],
                },
                kind="read",
                source="intern",
            ),
            None,
            "allow",
        )
        return ToolSet(out, {n: visible[n] for n in deferrable})

    @staticmethod
    def search(toolset: ToolSet, query: str, ctx: ToolContext, limit: int = 8) -> ToolResult:
        candidates = toolset.deferred
        words = [w for w in re.findall(r"\w{3,}", query.lower())]
        scored = []
        for name, r in candidates.items():
            hay = f"{name} {r.spec.title} {r.spec.description}".lower()
            score = sum(hay.count(w[:6]) for w in words)
            if score:
                scored.append((score, name, r))
        scored.sort(key=lambda x: -x[0])
        hits = scored[:limit]
        if not hits:
            return ToolResult("Keine passenden Tools gefunden. Formuliere die Suche anders.")
        lines = []
        for _, name, r in hits:
            ctx.activated.add(name)
            lines.append(f"- {name}: {r.spec.description[:300]}")
        return ToolResult(
            "Diese Tools sind ab jetzt freigeschaltet und können direkt aufgerufen werden:\n" + "\n".join(lines)
        )

    @staticmethod
    def definitions(tools: dict[str, ResolvedTool]) -> list[dict[str, Any]]:
        return [r.spec.definition() for r in tools.values()]


def short_args(args: dict[str, Any], limit: int = 120) -> str:
    text = json.dumps(args, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"

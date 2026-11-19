"""Basistypen der Tool-Schicht."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

# read   = liest nur (immer erlaubt, auch im Plan-Modus)
# write  = verändert etwas außerhalb des Harness (Freigabe je nach Modus)
# memory = schreibt nur ins eigene Gedächtnis (erlaubt, außer im Inkognito-Modus)
ToolKind = Literal["read", "write", "memory"]
Policy = Literal["allow", "ask", "deny"]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    kind: ToolKind = "write"
    source: str = "intern"
    title: str = ""
    policy: Policy | None = None  # explizite Regel aus der Konfiguration (überstimmt den Modus)

    def definition(self) -> dict[str, Any]:
        params = self.parameters if isinstance(self.parameters, dict) else {}
        if params.get("type") != "object":
            params = {"type": "object", "properties": {}, **{k: v for k, v in params.items() if k != "type"}}
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description[:1024], "parameters": params},
        }


@dataclass
class ToolResult:
    text: str
    is_error: bool = False


@dataclass
class ToolContext:
    conversation_id: str
    incognito: bool = False
    permission_mode: str = "ask"
    activated: set[str] = field(default_factory=set)  # per tool_search freigeschaltete Tools
    allowed_tools: set[str] = field(default_factory=set)  # „für diesen Chat erlaubt“
    memory_written: bool = False


class ToolProvider(Protocol):
    def list_tools(self, ctx: ToolContext) -> list[ToolSpec]: ...

    async def call(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult: ...

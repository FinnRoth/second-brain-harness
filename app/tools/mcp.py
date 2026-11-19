"""MCP-Tools als ToolProvider."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

from ..mcp.connection import ServerUnavailable
from ..mcp.manager import MCPManager
from .base import ToolContext, ToolResult, ToolSpec

log = logging.getLogger(__name__)


def exposed_name(server: str, tool: str) -> str:
    """mcp__<server>__<tool>, bereinigt auf ^[A-Za-z0-9_-]{1,64}$ (OpenAI-Limit)."""
    name = "mcp__" + re.sub(r"[^A-Za-z0-9_-]", "_", server) + "__" + re.sub(r"[^A-Za-z0-9_-]", "_", tool)
    if len(name) > 64:
        digest = hashlib.sha1(name.encode()).hexdigest()[:6]
        name = name[:57] + "_" + digest
    return name


def result_to_text(result: Any, max_chars: int) -> ToolResult:
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        btype = getattr(block, "type", "")
        if btype == "text":
            parts.append(block.text)
        elif btype == "image":
            parts.append(f"[Bild ({block.mime_type}, {len(block.data) * 3 // 4} Bytes) – Bilder werden noch nicht weitergereicht]")
        elif btype == "audio":
            parts.append(f"[Audio ({block.mime_type})]")
        elif btype == "resource":
            res = block.resource
            text = getattr(res, "text", None)
            parts.append(f"[Ressource {res.uri}]\n{text}" if text else f"[Binäre Ressource {res.uri}]")
        elif btype == "resource_link":
            parts.append(f"[Link: {getattr(block, 'name', '')} {block.uri}]")
        else:
            parts.append(str(block))
    structured = getattr(result, "structured_content", None)
    if structured is not None and not any(p.strip() for p in parts):
        parts.append(json.dumps(structured, ensure_ascii=False, indent=1))
    text = "\n".join(parts).strip() or "(keine Ausgabe)"
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n[… gekürzt, {len(text) - max_chars} weitere Zeichen]"
    is_error = bool(getattr(result, "is_error", False))
    return ToolResult(("Fehler: " + text) if is_error else text, is_error=is_error)


class MCPToolProvider:
    def __init__(self, manager: MCPManager, max_output_chars: int = 20000) -> None:
        self.manager = manager
        self.max_output_chars = max_output_chars
        self._names: dict[str, tuple[str, str]] = {}

    def list_tools(self, ctx: ToolContext) -> list[ToolSpec]:
        specs: list[ToolSpec] = []
        names: dict[str, tuple[str, str]] = {}
        for server, conn in self.manager.connections.items():
            if conn.status != "ready":
                continue
            rules = conn.cfg.rules
            for tool in conn.tools:
                name = exposed_name(server, tool.name)
                names[name] = (server, tool.name)
                ann = tool.annotations
                read_only = bool(ann and ann.read_only_hint) or rules.is_read_only(tool.name)
                title = tool.title or (ann.title if ann and ann.title else "") or tool.name
                specs.append(
                    ToolSpec(
                        name=name,
                        description=f"[MCP: {server}] {tool.description or title}",
                        parameters=tool.input_schema or {"type": "object", "properties": {}},
                        kind="read" if read_only else "write",
                        source=f"mcp:{server}",
                        title=title,
                        policy=rules.policy(tool.name),
                    )
                )
        self._names = names
        return specs

    def resolve(self, name: str) -> tuple[str, str] | None:
        return self._names.get(name)

    async def call(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        target = self._names.get(name)
        if not target:
            return ToolResult(f"Fehler: MCP-Tool '{name}' ist nicht (mehr) verfügbar.", is_error=True)
        server, tool = target
        conn = self.manager.connections.get(server)
        if not conn:
            return ToolResult(f"Fehler: MCP-Server '{server}' ist nicht konfiguriert.", is_error=True)
        try:
            result = await conn.call_tool(tool, args)
        except ServerUnavailable as e:
            return ToolResult(f"Fehler: {e}", is_error=True)
        except Exception as e:  # Fehler des Servers als Tool-Ergebnis zurückgeben
            log.warning("MCP-Tool %s/%s fehlgeschlagen: %s", server, tool, e)
            return ToolResult(f"Fehler beim Aufruf von {server}/{tool}: {type(e).__name__}: {e}", is_error=True)
        return result_to_text(result, self.max_output_chars)

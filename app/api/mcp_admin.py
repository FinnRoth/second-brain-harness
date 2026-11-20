"""Admin-API für MCP-Server und Tool-Freigaben."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..mcp import editor
from ..mcp.config import ConfigError
from ..tools.base import ToolContext
from .deps import get_services, require_api_key

router = APIRouter(tags=["mcp"], dependencies=[Depends(require_api_key)])


@router.get("/mcp/servers")
async def list_servers(request: Request):
    """Status aller MCP-Server inkl. Tools und Fehlermeldungen."""
    return get_services(request).mcp.status()


@router.post("/mcp/reload")
async def reload_config(request: Request):
    """Config neu laden (passiert auch automatisch bei Dateiänderungen)."""
    s = get_services(request)
    changes = await s.mcp.reload()
    await s.mcp.wait_until_settled(10)
    return {"changes": changes, **s.mcp.status()}


@router.post("/mcp/servers/{name}/restart")
async def restart_server(request: Request, name: str):
    s = get_services(request)
    if not await s.mcp.restart(name):
        raise HTTPException(404, "Unbekannter Server")
    await s.mcp.wait_until_settled(10)
    return s.mcp.connections[name].describe()


@router.get("/mcp/servers/{name}/logs")
async def server_logs(request: Request, name: str, lines: int = 100):
    conn = get_services(request).mcp.connections.get(name)
    if not conn:
        raise HTTPException(404, "Unbekannter Server")
    return {"name": name, "log": conn.log_tail(lines)}


@router.post("/mcp/servers/{name}/tools/{tool}")
async def call_tool(request: Request, name: str, tool: str, arguments: dict | None = None):
    """Ruft ein MCP-Tool direkt auf (zum Testen, ohne LLM und ohne Freigabe-Logik)."""
    s = get_services(request)
    conn = s.mcp.connections.get(name)
    if not conn:
        raise HTTPException(404, "Unbekannter Server")
    from ..tools.mcp import result_to_text

    try:
        result = await conn.call_tool(tool, arguments or {})
    except Exception as e:
        raise HTTPException(502, f"{type(e).__name__}: {e}") from e
    r = result_to_text(result, s.settings.mcp_max_output_chars)
    return {"is_error": r.is_error, "text": r.text}


@router.get("/tools")
async def list_tools(request: Request, mode: str = "ask", incognito: bool = False):
    """Welche Tools sieht das Modell in einem Modus – und was ist freigabepflichtig?"""
    s = get_services(request)
    ctx = ToolContext(conversation_id="preview", incognito=incognito, permission_mode=mode)
    toolset = s.tools.resolve(ctx)
    return {
        "mode": mode,
        "tools": [
            {"name": n, "source": r.spec.source, "kind": r.spec.kind, "decision": r.decision}
            for n, r in toolset.tools.items()
        ],
        "deferred": sorted(toolset.deferred),
    }


@router.get("/approvals/{conversation_id}")
async def pending_approvals(request: Request, conversation_id: str):
    return get_services(request).store.pending_approvals(conversation_id)


# ---------------------------------------------------------- Konfiguration (App)
class ServerBody(BaseModel):
    name: str
    previous_name: str | None = None
    config: dict[str, Any]


class SecretBody(BaseModel):
    value: str


def _paths(request: Request) -> tuple[str, str | None]:
    s = get_services(request).settings
    return s.mcp_config, s.mcp_secrets or None


async def _apply(request: Request) -> dict[str, Any]:
    s = get_services(request)
    changes = await s.mcp.reload()
    await s.mcp.wait_until_settled(15)
    return {"changes": changes, **s.mcp.status()}


@router.get("/mcp/config")
async def get_config(request: Request):
    """Rohe Konfiguration (mit ${VAR}-Platzhaltern) + Secrets-Übersicht (ohne Werte)."""
    config_path, secrets_path = _paths(request)
    try:
        raw = editor.read_raw(config_path)
    except ConfigError as e:
        raise HTTPException(400, str(e)) from e
    return {
        "path": config_path,
        "secrets_path": secrets_path,
        "servers": raw["mcpServers"],
        "secrets": editor.list_secrets(secrets_path, config_path),
    }


@router.put("/mcp/config/servers")
async def put_server(request: Request, body: ServerBody):
    config_path, _ = _paths(request)
    try:
        editor.set_server(config_path, body.name.strip(), body.config, body.previous_name)
    except ConfigError as e:
        raise HTTPException(400, str(e)) from e
    return await _apply(request)


@router.delete("/mcp/config/servers/{name}")
async def remove_server(request: Request, name: str):
    config_path, _ = _paths(request)
    if not editor.delete_server(config_path, name):
        raise HTTPException(404, "Unbekannter Server")
    return await _apply(request)


@router.post("/mcp/config/import")
async def import_config(request: Request, snippet: dict[str, Any]):
    """JSON aus Claude Code / Claude Desktop übernehmen ({"mcpServers": {...}})."""
    config_path, _ = _paths(request)
    try:
        names = editor.import_servers(config_path, snippet)
    except ConfigError as e:
        raise HTTPException(400, str(e)) from e
    return {"imported": names, **(await _apply(request))}


@router.put("/mcp/secrets/{name}")
async def put_secret(request: Request, name: str, body: SecretBody):
    _, secrets_path = _paths(request)
    if not secrets_path:
        raise HTTPException(400, "MCP_SECRETS ist nicht gesetzt")
    try:
        editor.set_secret(secrets_path, name, body.value)
    except ConfigError as e:
        raise HTTPException(400, str(e)) from e
    await _apply(request)
    return {"name": name, "set": True}


@router.delete("/mcp/secrets/{name}")
async def delete_secret(request: Request, name: str):
    _, secrets_path = _paths(request)
    if not secrets_path:
        raise HTTPException(400, "MCP_SECRETS ist nicht gesetzt")
    editor.set_secret(secrets_path, name, None)
    await _apply(request)
    return {"name": name, "set": False}

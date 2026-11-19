"""Admin-API für MCP-Server und Tool-Freigaben."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

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

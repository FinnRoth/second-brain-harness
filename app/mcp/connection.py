"""Eine Verbindung zu einem MCP-Server.

Pro Server gibt es genau EINEN langlebigen asyncio-Task („Owner“), der Prozess
bzw. HTTP-Verbindung und MCP-Session öffnet, hält und wieder schließt. Das ist
nötig, weil die anyio-Transports der MCP-SDK im selben Task betreten und
verlassen werden müssen. Tool-Aufrufe aus anderen Tasks laufen über die offene
Session (JSON-RPC ist nebenläufig).

Fällt der Server aus (Prozess beendet, Ping schlägt fehl), startet der Owner
ihn mit exponentiellem Backoff neu.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path
from typing import Any

import mcp_types as mtypes
from mcp import Client
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from .config import ServerConfig

log = logging.getLogger(__name__)

CLIENT_INFO = mtypes.Implementation(name="second-brain-harness", version="0.2.0")
PING_INTERVAL = 30.0
MAX_BACKOFF = 300.0


class ServerUnavailable(RuntimeError):
    pass


class ServerConnection:
    def __init__(self, cfg: ServerConfig, log_dir: Path, connect_timeout: float = 120.0) -> None:
        self.cfg = cfg
        self.name = cfg.name
        self.log_path = log_dir / f"{cfg.name}.log"
        self.connect_timeout = connect_timeout
        self.status = "stopped"  # stopped | starting | ready | failed | disabled
        self.error: str | None = None
        self.tools: list[mtypes.Tool] = []
        self.server_info: dict[str, Any] = {}
        self.instructions: str | None = None
        self.started_at: float | None = None
        self.restarts = 0
        self._client: Client | None = None
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._attempted = asyncio.Event()  # erster Verbindungsversuch abgeschlossen (Erfolg oder Fehler)
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._refresh_tools = False

    # ------------------------------------------------------------- Lebenszyklus
    def start(self) -> None:
        if not self.cfg.enabled:
            self.status = "disabled"
            return
        if self._task is None or self._task.done():
            self._stop.clear()
            self.status = "starting"
            self._task = asyncio.create_task(self._run(), name=f"mcp:{self.name}")

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=15)
            except (asyncio.TimeoutError, Exception):
                self._task.cancel()
                with contextlib.suppress(BaseException):
                    await self._task
            self._task = None
        self.status = "disabled" if not self.cfg.enabled else "stopped"

    async def wait_settled(self, timeout: float) -> None:
        """Wartet, bis der erste Startversuch abgeschlossen ist (für den Harness-Start)."""
        if self.status in ("disabled", "stopped"):
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._attempted.wait(), timeout=timeout)

    async def wait_ready(self, timeout: float) -> bool:
        if self.status == "ready":
            return True
        if self.status in ("disabled", "stopped"):
            return False
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return self.status == "ready"

    def _transport(self, errlog):
        c = self.cfg
        if c.transport == "stdio":
            params = StdioServerParameters(command=c.command, args=c.args, env=c.env or None, cwd=c.cwd)
            return stdio_client(params, errlog=errlog)
        if c.transport == "sse":
            return sse_client(c.url, headers=c.headers or None)
        return streamable_http_client(c.url, http_client=create_mcp_http_client(headers=c.headers or None))

    async def _on_message(self, message: Any) -> None:
        msg = getattr(message, "root", message)
        if isinstance(msg, Exception):
            log.debug("MCP %s: Transport-Meldung %s", self.name, msg)
            return
        if getattr(msg, "method", None) == "notifications/tools/list_changed":
            self._refresh_tools = True
            self._wake.set()

    async def _load_tools(self, client: Client) -> None:
        tools: list[mtypes.Tool] = []
        cursor = None
        for _ in range(50):
            page = await client.list_tools(cursor=cursor, cache_mode="refresh")
            tools.extend(page.tools)
            cursor = page.next_cursor
            if not cursor:
                break
        self.tools = tools

    async def _run(self) -> None:
        backoff = 2.0
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        while not self._stop.is_set():
            self.status = "starting"
            self._ready.clear()
            try:
                with open(self.log_path, "a", encoding="utf-8", buffering=1) as errlog:
                    errlog.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} Start ({self.cfg.transport}) ---\n")
                    client = Client(
                        self._transport(errlog),
                        client_info=CLIENT_INFO,
                        message_handler=self._on_message,
                        read_timeout_seconds=self.cfg.timeout,
                    )
                    async with asyncio.timeout(self.connect_timeout):
                        await client.__aenter__()
                    try:
                        self._client = client
                        await self._load_tools(client)
                        info = client.server_info
                        self.server_info = info.model_dump(exclude_none=True) if info else {}
                        self.instructions = client.instructions
                        self.status, self.error, self.started_at = "ready", None, time.time()
                        self._ready.set()
                        self._attempted.set()
                        backoff = 2.0
                        log.info("MCP-Server %s bereit (%d Tools)", self.name, len(self.tools))
                        await self._supervise(client)
                    finally:
                        self._client = None
                        self._ready.clear()
                        with contextlib.suppress(BaseException):
                            await client.__aexit__(None, None, None)
            except asyncio.CancelledError:
                raise
            except BaseException as e:  # noqa: BLE001 – Server-Fehler dürfen den Harness nie stoppen
                err = _describe(e)
                self.status, self.error = "failed", err
                self._attempted.set()
                log.warning("MCP-Server %s: %s", self.name, err)
            if self._stop.is_set():
                break
            self.restarts += 1
            self._wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            backoff = min(backoff * 2, MAX_BACKOFF)
        self.status = "stopped"

    async def _supervise(self, client: Client) -> None:
        """Hält die Verbindung, bis gestoppt wird oder der Server nicht mehr antwortet."""
        while not self._stop.is_set():
            self._wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=PING_INTERVAL)
            if self._stop.is_set():
                return
            if self._refresh_tools:
                self._refresh_tools = False
                await self._load_tools(client)
                log.info("MCP-Server %s: Tool-Liste aktualisiert (%d Tools)", self.name, len(self.tools))
                continue
            # Gesundheitscheck: tools/list funktioniert in allen Protokollversionen
            # (ping gibt es ab 2026-07-28 nicht mehr) und hält nebenbei die Tool-Liste aktuell.
            async with asyncio.timeout(15):
                await self._load_tools(client)

    # -------------------------------------------------------------------- Aufruf
    async def call_tool(self, tool: str, arguments: dict[str, Any]) -> mtypes.CallToolResult:
        if self.status != "ready":
            await self.wait_ready(timeout=min(self.connect_timeout, 30))
        client = self._client
        if client is None or self.status != "ready":
            raise ServerUnavailable(f"MCP-Server '{self.name}' ist nicht verfügbar ({self.status}: {self.error or '-'})")
        try:
            return await client.call_tool(tool, arguments, read_timeout_seconds=self.cfg.timeout)
        except Exception:
            # Möglicherweise ist der Server weg → sofort per Ping prüfen statt erst beim nächsten Intervall
            self._wake.set()
            raise

    def log_tail(self, lines: int = 60) -> str:
        if not self.log_path.exists():
            return ""
        with open(self.log_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 64_000))
            data = f.read().decode("utf-8", "replace")
        return "\n".join(data.splitlines()[-lines:])

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "transport": self.cfg.transport,
            "command": " ".join([self.cfg.command or "", *self.cfg.args]).strip() or None,
            "url": self.cfg.url,
            "status": self.status,
            "error": self.error,
            "missing_vars": self.cfg.missing_vars,
            "server_info": self.server_info,
            "restarts": self.restarts,
            "tools": [
                {
                    "name": t.name,
                    "description": (t.description or "")[:200],
                    "read_only_hint": bool(t.annotations and t.annotations.read_only_hint),
                    "policy": self.cfg.rules.policy(t.name),
                }
                for t in self.tools
            ],
        }


def _describe(e: BaseException) -> str:
    if isinstance(e, BaseExceptionGroup):
        inner = [x for x in e.exceptions]
        if inner:
            return _describe(inner[0])
    if isinstance(e, TimeoutError | asyncio.TimeoutError):
        return "Zeitüberschreitung beim Verbinden"
    if isinstance(e, FileNotFoundError):
        return f"Befehl nicht gefunden: {e.filename or e}"
    return f"{type(e).__name__}: {e}"[:500]

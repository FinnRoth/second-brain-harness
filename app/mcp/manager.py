"""Verwaltet alle MCP-Server: Config laden, starten, Hot-Reload, Status."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

from .config import ConfigError, ServerConfig, load_config
from .connection import ServerConnection

log = logging.getLogger(__name__)


class MCPManager:
    def __init__(
        self,
        config_path: str,
        secrets_path: str | None,
        log_dir: str,
        connect_timeout: float = 120.0,
        watch_interval: float = 5.0,
    ) -> None:
        self.config_path = Path(config_path).expanduser() if config_path else None
        self.secrets_path = secrets_path
        self.log_dir = Path(log_dir).expanduser()
        self.connect_timeout = connect_timeout
        self.watch_interval = watch_interval
        self.connections: dict[str, ServerConnection] = {}
        self.config_error: str | None = None
        self._fingerprints: dict[str, str] = {}
        self._mtime: float | None = None
        self._watch_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self.config_path is not None

    def _file_mtime(self) -> float | None:
        files = [self.config_path] + ([Path(self.secrets_path).expanduser()] if self.secrets_path else [])
        mtimes = [f.stat().st_mtime for f in files if f and f.exists()]
        return max(mtimes) if mtimes else None

    async def start(self, wait: float = 0.0) -> None:
        if not self.enabled:
            return
        await self.reload()
        if wait > 0:
            await self.wait_until_settled(wait)
        if self.watch_interval > 0 and self._watch_task is None:
            self._watch_task = asyncio.create_task(self._watch(), name="mcp-config-watch")

    async def wait_until_settled(self, timeout: float) -> None:
        pending = [c.wait_settled(timeout) for c in self.connections.values() if c.status == "starting"]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def stop(self) -> None:
        if self._watch_task:
            self._watch_task.cancel()
            with contextlib.suppress(BaseException):
                await self._watch_task
            self._watch_task = None
        await asyncio.gather(*(c.stop() for c in self.connections.values()), return_exceptions=True)

    async def reload(self) -> dict[str, list[str]]:
        """Liest die Config neu und startet nur geänderte/neue Server (neu)."""
        async with self._lock:
            self._mtime = self._file_mtime()
            try:
                configs: dict[str, ServerConfig] = load_config(self.config_path, self.secrets_path)
                self.config_error = None
            except ConfigError as e:
                self.config_error = str(e)
                log.error("MCP-Konfiguration fehlerhaft: %s", e)
                return {"error": [str(e)]}

            changes: dict[str, list[str]] = {"started": [], "restarted": [], "stopped": []}
            for name in list(self.connections):
                if name not in configs:
                    await self.connections.pop(name).stop()
                    self._fingerprints.pop(name, None)
                    changes["stopped"].append(name)
            for name, cfg in configs.items():
                for var in cfg.missing_vars:
                    log.warning("MCP-Server %s: Variable ${%s} ist nicht gesetzt", name, var)
                fp = cfg.fingerprint()
                conn = self.connections.get(name)
                if conn and self._fingerprints.get(name) == fp:
                    conn.cfg = cfg  # Tool-Regeln/Timeout ohne Neustart übernehmen
                    continue
                if conn:
                    await conn.stop()
                    changes["restarted"].append(name)
                else:
                    changes["started"].append(name)
                conn = ServerConnection(cfg, self.log_dir, self.connect_timeout)
                self.connections[name] = conn
                self._fingerprints[name] = fp
                conn.start()
            if any(changes.values()):
                log.info("MCP-Konfiguration geladen: %s", {k: v for k, v in changes.items() if v})
            return changes

    async def restart(self, name: str) -> bool:
        conn = self.connections.get(name)
        if not conn:
            return False
        await conn.stop()
        new = ServerConnection(conn.cfg, self.log_dir, self.connect_timeout)
        self.connections[name] = new
        new.start()
        return True

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(self.watch_interval)
            try:
                if self._file_mtime() != self._mtime:
                    log.info("MCP-Konfiguration geändert – lade neu")
                    await self.reload()
            except Exception:
                log.exception("Fehler beim Neuladen der MCP-Konfiguration")

    def status(self) -> dict[str, Any]:
        return {
            "config_path": str(self.config_path) if self.config_path else None,
            "config_error": self.config_error,
            "servers": [c.describe() for c in self.connections.values()],
        }

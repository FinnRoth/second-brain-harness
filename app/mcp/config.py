"""MCP-Konfiguration – kompatibel zum Format von Claude Code / Claude Desktop.

{
  "mcpServers": {
    "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
               "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}},
    "linear": {"type": "http", "url": "https://mcp.linear.app/mcp",
               "headers": {"Authorization": "Bearer ${LINEAR_TOKEN}"}},

    // optionale Harness-Erweiterungen:
    "dateien": {"command": "npx", "args": [...],
                "disabled": false,
                "timeout": 60,
                "tools": {"deny": ["move_file"], "ask": ["write_file"], "allow": [], "readOnly": ["search"]}}
  }
}

`${VAR}` bzw. `${VAR:-default}` werden aus der Umgebung und der Secrets-Datei ersetzt.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(ValueError):
    pass


def load_dotenv(path: str | Path | None) -> dict[str, str]:
    """Minimaler .env-Parser für die Secrets-Datei (KEY=VALUE, # Kommentare, optionale Quotes)."""
    if not path or not Path(path).expanduser().exists():
        return {}
    out: dict[str, str] = {}
    for raw in Path(path).expanduser().read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def expand(value: Any, env: dict[str, str], missing: set[str]) -> Any:
    if isinstance(value, str):

        def repl(m: re.Match) -> str:
            name, default = m.group(1), m.group(2)
            if name in env:
                return env[name]
            if default is not None:
                return default
            missing.add(name)
            return ""

        return _VAR_RE.sub(repl, value)
    if isinstance(value, list):
        return [expand(v, env, missing) for v in value]
    if isinstance(value, dict):
        return {k: expand(v, env, missing) for k, v in value.items()}
    return value


@dataclass
class ToolRules:
    allow: list[str] = field(default_factory=list)
    ask: list[str] = field(default_factory=list)
    deny: list[str] = field(default_factory=list)
    read_only: list[str] = field(default_factory=list)

    @staticmethod
    def _match(name: str, patterns: list[str]) -> bool:
        for p in patterns:
            if p == "*" or p == name:
                return True
            if p.endswith("*") and name.startswith(p[:-1]):
                return True
        return False

    def policy(self, tool: str) -> str | None:
        if self._match(tool, self.deny):
            return "deny"
        if self._match(tool, self.ask):
            return "ask"
        if self._match(tool, self.allow):
            return "allow"
        return None

    def is_read_only(self, tool: str) -> bool:
        return self._match(tool, self.read_only)


@dataclass
class ServerConfig:
    name: str
    transport: str  # stdio | http | sse
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    timeout: float = 60.0
    rules: ToolRules = field(default_factory=ToolRules)
    missing_vars: list[str] = field(default_factory=list)

    def fingerprint(self) -> str:
        """Ändert sich, wenn ein Neustart nötig ist."""
        return json.dumps(
            [self.transport, self.command, self.args, self.env, self.cwd, self.url, self.headers, self.enabled],
            sort_keys=True,
        )


def _expand_path(p: str | None) -> str | None:
    return os.path.expanduser(p) if p else p


def parse_config(data: dict[str, Any], env: dict[str, str]) -> dict[str, ServerConfig]:
    servers = data.get("mcpServers")
    if servers is None:
        servers = data.get("servers", {})
    if not isinstance(servers, dict):
        raise ConfigError("'mcpServers' muss ein Objekt sein")
    out: dict[str, ServerConfig] = {}
    for name, raw in servers.items():
        if not isinstance(raw, dict):
            raise ConfigError(f"Server '{name}': Eintrag muss ein Objekt sein")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", name):
            raise ConfigError(f"Server-Name '{name}' ist ungültig (erlaubt: Buchstaben, Ziffern, _ . -, max. 40)")
        missing: set[str] = set()
        cfg = expand(raw, env, missing)
        transport = (cfg.get("type") or cfg.get("transport") or ("stdio" if cfg.get("command") else "http")).lower()
        if transport in ("streamable-http", "streamable_http", "streamablehttp"):
            transport = "http"
        if transport not in ("stdio", "http", "sse"):
            raise ConfigError(f"Server '{name}': unbekannter Typ '{transport}'")
        if transport == "stdio" and not cfg.get("command"):
            raise ConfigError(f"Server '{name}': 'command' fehlt")
        if transport != "stdio" and not cfg.get("url"):
            raise ConfigError(f"Server '{name}': 'url' fehlt")
        tools = cfg.get("tools") or {}
        rules = ToolRules(
            allow=list(tools.get("allow") or []),
            ask=list(tools.get("ask") or []),
            deny=list(tools.get("deny") or []),
            read_only=list(tools.get("readOnly") or tools.get("read_only") or []),
        )
        enabled = bool(cfg.get("enabled", True)) and not bool(cfg.get("disabled", False))
        out[name] = ServerConfig(
            name=name,
            transport=transport,
            command=_expand_path(cfg.get("command")),
            args=[str(a) for a in cfg.get("args") or []],
            env={k: str(v) for k, v in (cfg.get("env") or {}).items()},
            cwd=_expand_path(cfg.get("cwd")),
            url=cfg.get("url"),
            headers={k: str(v) for k, v in (cfg.get("headers") or {}).items()},
            enabled=enabled,
            timeout=float(cfg.get("timeout") or 60),
            rules=rules,
            missing_vars=sorted(missing),
        )
    return out


def load_config(path: str | Path, secrets_path: str | Path | None = None) -> dict[str, ServerConfig]:
    p = Path(path).expanduser()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(f"{p}: ungültiges JSON ({e})") from e
    env = {**os.environ, **load_dotenv(secrets_path)}
    return parse_config(data, env)

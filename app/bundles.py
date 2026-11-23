"""Pakete: ausgewählte Konfiguration exportieren und anderswo importieren.

Ein Paket ist eine JSON-Datei mit MCP-Servern (wie in mcp.json, Secrets nur als ${NAME}),
Nachschlagewerken, optional Tool-Regeln und eigenen Anweisungen. Secret-Werte werden
nie exportiert; beim Import zeigt die Vorschau, welche fehlen.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .mcp import editor
from .mcp.config import ConfigError, load_dotenv
from .tools.mcp import exposed_name

if TYPE_CHECKING:
    from .services import Services

FORMAT = "second-brain-bundle"
VERSION = 1
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}")
_ABS_PATH_RE = re.compile(r"(^|[\s=\"'])(~/|/Users/|/home/|/opt/|/var/|/tmp/|[A-Za-z]:\\)")


class BundleError(ValueError):
    pass


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    return []


def path_warnings(raw: dict[str, Any]) -> list[str]:
    """Werte mit rechnerspezifischen Pfaden, die nach dem Import angepasst werden müssen."""
    fields = {"command": raw.get("command"), "args": raw.get("args"), "cwd": raw.get("cwd"), "env": raw.get("env")}
    hits = []
    for key, value in fields.items():
        for s in _strings(value):
            if _ABS_PATH_RE.search(s) or s.startswith("/"):
                hits.append(f"{key}: {s}")
    return hits


def _unique(name: str, taken: set[str], sep: str = "-") -> str:
    i = 2
    candidate = f"{name}{sep}{i}"
    while candidate in taken:
        i += 1
        candidate = f"{name}{sep}{i}"
    return candidate


# ------------------------------------------------------------------- Export
def exportable(s: "Services") -> dict[str, Any]:
    try:
        servers = editor.read_raw(s.settings.mcp_config)["mcpServers"]
    except ConfigError:
        servers = {}
    policies = s.store.tool_policies()
    out_servers = []
    for name, raw in servers.items():
        conn = s.mcp.connections.get(name)
        tools = [t.name for t in conn.tools] if conn else []
        out_servers.append(
            {
                "name": name,
                "summary": " ".join([str(raw.get("command") or raw.get("url") or ""), *map(str, raw.get("args") or [])]).strip(),
                "secrets": sorted(set(_VAR_RE.findall(json.dumps(raw)))),
                "tools": len(tools),
                "policies": sum(1 for t in tools if exposed_name(name, t) in policies),
                "path_warnings": path_warnings(raw),
            }
        )
    refs = [
        {"id": d["id"], "title": d["title"], "description": d["description"], "chars": d["chars"], "mode": d["mode"]}
        for d in s.store.list_reference_docs()
    ]
    return {
        "servers": out_servers,
        "references": refs,
        "custom_instructions": bool(s.store.get_custom_instructions()),
    }


def build(
    s: "Services",
    *,
    name: str,
    description: str = "",
    servers: list[str] | None = None,
    references: list[int] | None = None,
    tool_policies: bool = True,
    custom_instructions: bool = False,
) -> dict[str, Any]:
    raw_servers = editor.read_raw(s.settings.mcp_config)["mcpServers"]
    bundle: dict[str, Any] = {
        "format": FORMAT,
        "version": VERSION,
        "name": name.strip() or "Second-Brain-Paket",
        "description": description.strip(),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mcpServers": {},
        "references": [],
    }
    policies = s.store.tool_policies()
    exported_policies: dict[str, dict[str, str]] = {}
    for server in servers or []:
        if server not in raw_servers:
            raise BundleError(f"Unbekannter MCP-Server „{server}“")
        bundle["mcpServers"][server] = raw_servers[server]
        if tool_policies:
            conn = s.mcp.connections.get(server)
            for tool in conn.tools if conn else []:
                rule = policies.get(exposed_name(server, tool.name))
                if rule:
                    exported_policies.setdefault(server, {})[tool.name] = rule
    for ref_id in references or []:
        doc = s.store.get_reference_doc(ref_id)
        if not doc:
            raise BundleError(f"Unbekanntes Nachschlagewerk #{ref_id}")
        bundle["references"].append(
            {k: doc[k] for k in ("title", "description", "content", "mode", "enabled")}
        )
    if exported_policies:
        bundle["toolPolicies"] = exported_policies
    if custom_instructions:
        bundle["customInstructions"] = s.store.get_custom_instructions()
    return bundle


# ------------------------------------------------------------------- Import
def normalize(data: Any) -> dict[str, Any]:
    """Akzeptiert Pakete und reine MCP-Configs ({"mcpServers": {...}} aus Claude Code)."""
    if not isinstance(data, dict):
        raise BundleError("Erwartet wird ein JSON-Objekt")
    if data.get("format") not in (None, FORMAT):
        raise BundleError(f"Unbekanntes Format „{data.get('format')}“")
    if data.get("format") == FORMAT and int(data.get("version") or 0) > VERSION:
        raise BundleError("Das Paket stammt aus einer neueren Version – bitte Second Brain aktualisieren")
    servers = data.get("mcpServers") or {}
    refs = data.get("references") or []
    if not isinstance(servers, dict) or not isinstance(refs, list):
        raise BundleError("Ungültiger Aufbau des Pakets")
    if not servers and not refs and not data.get("customInstructions"):
        raise BundleError("Das Paket ist leer")
    for name, raw in servers.items():
        if not isinstance(raw, dict):
            raise BundleError(f"Server „{name}“: Eintrag muss ein Objekt sein")
        try:
            editor.validate_server(name, editor.clean_server(raw))
        except ConfigError as e:
            raise BundleError(f"Server „{name}“: {e}") from e
    for i, r in enumerate(refs):
        if not isinstance(r, dict) or not str(r.get("title") or "").strip():
            raise BundleError(f"Nachschlagewerk {i + 1}: Titel fehlt")
    return {
        "name": data.get("name") or "Import",
        "description": data.get("description") or "",
        "mcpServers": servers,
        "references": refs,
        "toolPolicies": data.get("toolPolicies") or {},
        "customInstructions": data.get("customInstructions") or "",
    }


def _local_secret_names(s: "Services") -> set[str]:
    import os

    values = load_dotenv(s.settings.mcp_secrets) if s.settings.mcp_secrets else {}
    return {k for k, v in values.items() if v} | {k for k in os.environ if os.environ[k]}


def preview(s: "Services", data: Any) -> dict[str, Any]:
    b = normalize(data)
    try:
        existing = editor.read_raw(s.settings.mcp_config)["mcpServers"]
    except ConfigError:
        existing = {}
    have = _local_secret_names(s)
    titles = {d["title"].lower(): d["id"] for d in s.store.list_reference_docs()}
    servers = []
    for name, raw in b["mcpServers"].items():
        needed = sorted(set(_VAR_RE.findall(json.dumps(raw))))
        servers.append(
            {
                "name": name,
                "exists": name in existing,
                "suggested_name": _unique(name, set(existing) | set(b["mcpServers"])) if name in existing else name,
                "summary": " ".join([str(raw.get("command") or raw.get("url") or ""), *map(str, raw.get("args") or [])]).strip(),
                "secrets": needed,
                "missing_secrets": [v for v in needed if v not in have],
                "path_warnings": path_warnings(raw),
                "policies": len(b["toolPolicies"].get(name) or {}),
            }
        )
    refs = []
    for i, r in enumerate(b["references"]):
        title = str(r["title"]).strip()
        refs.append(
            {
                "index": i,
                "title": title,
                "description": r.get("description") or "",
                "chars": len(r.get("content") or ""),
                "mode": r.get("mode") if r.get("mode") in ("always", "on_demand") else "on_demand",
                "exists": title.lower() in titles,
                "suggested_title": f"{title} (2)" if title.lower() in titles else title,
            }
        )
    missing = sorted({m for srv in servers for m in srv["missing_secrets"]})
    return {
        "name": b["name"],
        "description": b["description"],
        "servers": servers,
        "references": refs,
        "custom_instructions": {
            "present": bool(b["customInstructions"]),
            "chars": len(b["customInstructions"]),
            "local_exists": bool(s.store.get_custom_instructions()),
        },
        "missing_secrets": missing,
    }


def apply(s: "Services", data: Any, decisions: dict[str, Any]) -> dict[str, Any]:
    """decisions = {servers: {name: {action, new_name}}, references: {index: {action, new_title}},
    custom_instructions: append|replace|skip, tool_policies: bool, secrets: {NAME: value}}"""
    b = normalize(data)
    result: dict[str, list[str]] = {"servers": [], "references": [], "skipped": [], "secrets": []}
    config_path = s.settings.mcp_config

    # Secrets zuerst, damit Server direkt mit ihnen starten
    for name, value in (decisions.get("secrets") or {}).items():
        if value:
            if not s.settings.mcp_secrets:
                raise BundleError("MCP_SECRETS ist nicht gesetzt – Secrets können nicht gespeichert werden")
            try:
                editor.set_secret(s.settings.mcp_secrets, name, value)
            except ConfigError as e:
                raise BundleError(f"Secret {name}: {e}") from e
            result["secrets"].append(name)

    try:
        existing = set(editor.read_raw(config_path)["mcpServers"])
    except ConfigError as e:
        raise BundleError(str(e)) from e
    server_decisions = decisions.get("servers") or {}
    for name, raw in b["mcpServers"].items():
        d = server_decisions.get(name) or {}
        action = d.get("action") or ("rename" if name in existing else "add")
        if action == "skip":
            result["skipped"].append(f"Server {name}")
            continue
        target = name
        if action == "rename" or (action == "add" and name in existing):
            target = (d.get("new_name") or "").strip() or _unique(name, existing)
            if target in existing:
                raise BundleError(f"Es gibt schon einen Server „{target}“")
        try:
            editor.set_server(config_path, target, raw)
        except ConfigError as e:
            raise BundleError(f"Server „{target}“: {e}") from e
        existing.add(target)
        result["servers"].append(target)
        if decisions.get("tool_policies", True):
            for tool, rule in (b["toolPolicies"].get(name) or {}).items():
                if rule in ("allow", "ask", "deny"):
                    s.store.set_tool_policy(exposed_name(target, tool), rule)

    titles = {d["title"].lower(): d["id"] for d in s.store.list_reference_docs()}
    ref_decisions = {str(k): v for k, v in (decisions.get("references") or {}).items()}
    for i, r in enumerate(b["references"]):
        d = ref_decisions.get(str(i)) or {}
        title = str(r["title"]).strip()
        exists = title.lower() in titles
        action = d.get("action") or ("rename" if exists else "add")
        mode = r.get("mode") if r.get("mode") in ("always", "on_demand") else "on_demand"
        content = str(r.get("content") or "")
        description = str(r.get("description") or "")
        if action == "skip":
            result["skipped"].append(f"Nachschlagewerk {title}")
            continue
        if action == "overwrite" and exists:
            s.store.update_reference_doc(
                titles[title.lower()], description=description, content=content, mode=mode, enabled=bool(r.get("enabled", True))
            )
        else:
            if exists or action == "rename":
                title = (d.get("new_title") or "").strip() or f"{title} (2)"
            s.store.create_reference_doc(title, description, content, mode, bool(r.get("enabled", True)))
            titles[title.lower()] = -1
        result["references"].append(title)

    instr = b["customInstructions"]
    choice = decisions.get("custom_instructions") or "skip"
    if instr and choice != "skip":
        current = s.store.get_custom_instructions()
        s.store.set_custom_instructions(instr if choice == "replace" or not current else f"{current}\n\n{instr}")
        result["custom_instructions"] = [choice]
    return result

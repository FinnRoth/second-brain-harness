"""Bearbeiten der MCP-Konfiguration (config/mcp.json) und der Secrets (config/secrets.env) aus der App.

Die Dateien bleiben im Claude-Code-Format und lassen sich weiterhin von Hand bearbeiten.
Secret-Werte werden nie zurückgegeben – nur, ob sie gesetzt sind.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .config import ConfigError, load_dotenv, parse_config

NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,40}")
SECRET_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}")


def _atomic_write(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        if mode is not None:
            os.chmod(tmp, mode)
        elif path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ------------------------------------------------------------------- mcp.json
def read_raw(path: str | Path) -> dict[str, Any]:
    p = Path(path).expanduser()
    if not p.exists():
        return {"mcpServers": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError as e:
        raise ConfigError(f"{p} ist kein gültiges JSON ({e}). Bitte von Hand korrigieren.") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{p}: Oberste Ebene muss ein Objekt sein")
    if "mcpServers" not in data:
        data["mcpServers"] = data.pop("servers", {}) if isinstance(data.get("servers"), dict) else {}
    return data


def write_raw(path: str | Path, data: dict[str, Any]) -> None:
    _atomic_write(Path(path).expanduser(), json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def clean_server(raw: dict[str, Any]) -> dict[str, Any]:
    """Leere Felder entfernen, damit die Datei aufgeräumt bleibt."""
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if value in (None, "", [], {}):
            continue
        if key == "tools" and isinstance(value, dict):
            value = {k: v for k, v in value.items() if v}
            if not value:
                continue
        if key == "disabled" and value is False:
            continue
        out[key] = value
    return out


def validate_server(name: str, raw: dict[str, Any]) -> None:
    if not NAME_RE.fullmatch(name):
        raise ConfigError("Name: nur Buchstaben, Ziffern, _ . - (max. 40 Zeichen)")
    # Variablen gelten bei der Prüfung als gesetzt – fehlende Secrets sind kein Formatfehler
    dummy_env = {m: "x" for m in _VAR_RE.findall(json.dumps(raw))}
    parse_config({"mcpServers": {name: raw}}, dummy_env)


def set_server(path: str | Path, name: str, raw: dict[str, Any], previous_name: str | None = None) -> None:
    raw = clean_server(raw)
    validate_server(name, raw)
    data = read_raw(path)
    servers: dict[str, Any] = data["mcpServers"]
    if previous_name and previous_name != name:
        if name in servers:
            raise ConfigError(f"Es gibt schon einen Server „{name}“")
        # Reihenfolge erhalten
        data["mcpServers"] = {(name if k == previous_name else k): (raw if k == previous_name else v) for k, v in servers.items()}
    else:
        servers[name] = raw
    write_raw(path, data)


def delete_server(path: str | Path, name: str) -> bool:
    data = read_raw(path)
    if name not in data["mcpServers"]:
        return False
    del data["mcpServers"][name]
    write_raw(path, data)
    return True


def import_servers(path: str | Path, snippet: Any) -> list[str]:
    """Nimmt {"mcpServers": {...}}, {"servers": {...}} oder direkt {name: {...}} entgegen."""
    if not isinstance(snippet, dict):
        raise ConfigError("Erwartet wird ein JSON-Objekt")
    servers = snippet.get("mcpServers") or snippet.get("servers") or snippet
    if not isinstance(servers, dict) or not servers:
        raise ConfigError("Keine Server gefunden")
    if any(k in servers for k in ("command", "url", "type")):
        raise ConfigError("Bitte mit Namen einfügen, z. B. {\"mein-server\": {\"command\": …}}")
    for name, raw in servers.items():
        if not isinstance(raw, dict):
            raise ConfigError(f"Server „{name}“: Eintrag muss ein Objekt sein")
        validate_server(name, clean_server(raw))
    data = read_raw(path)
    for name, raw in servers.items():
        data["mcpServers"][name] = clean_server(raw)
    write_raw(path, data)
    return list(servers)


def referenced_vars(path: str | Path) -> set[str]:
    try:
        return set(_VAR_RE.findall(json.dumps(read_raw(path))))
    except ConfigError:
        return set()


# ---------------------------------------------------------------- secrets.env
def list_secrets(path: str | Path | None, config_path: str | Path) -> list[dict[str, Any]]:
    values = load_dotenv(path) if path else {}
    used = referenced_vars(config_path)
    names = sorted(set(values) | used)
    return [
        {
            "name": n,
            "set": bool(values.get(n)) or (n not in values and bool(os.environ.get(n))),
            "source": "secrets" if n in values else ("umgebung" if os.environ.get(n) else None),
            "used": n in used,
        }
        for n in names
    ]


def _quote(value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ConfigError("Secrets dürfen keine Zeilenumbrüche enthalten")
    if re.fullmatch(r"[A-Za-z0-9_./:@+=,-]*", value):
        return value
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    raise ConfigError("Secret enthält sowohl ' als auch \" – bitte direkt in secrets.env eintragen")


def set_secret(path: str | Path, name: str, value: str | None) -> None:
    """Setzt (value) oder entfernt (None) ein Secret; Kommentare und andere Zeilen bleiben erhalten."""
    if not SECRET_RE.fullmatch(name):
        raise ConfigError("Name: Buchstaben, Ziffern und _ (nicht mit Ziffer beginnen)")
    p = Path(path).expanduser()
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    out, found = [], False
    for line in lines:
        stripped = line.strip()
        key = stripped[7:] if stripped.startswith("export ") else stripped
        if "=" in key and key.split("=", 1)[0].strip() == name and not stripped.startswith("#"):
            found = True
            if value is not None:
                out.append(f"{name}={_quote(value)}")
            continue
        out.append(line)
    if value is not None and not found:
        out.append(f"{name}={_quote(value)}")
    _atomic_write(p, "\n".join(out) + "\n", mode=0o600)

"""Skills als Tools: use_skill, read_skill_file und (optional) run_skill_script."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..skills import SkillError, SkillStore, is_text
from .base import ToolContext, ToolResult, ToolSpec
from .references import search_lines

if TYPE_CHECKING:
    from ..documents import DocumentStore

MAX_FILE_CHARS = 60_000
MAX_OUTPUT_CHARS = 20_000
SCRIPT_TIMEOUT = 180
INTERPRETERS = {".py": [sys.executable], ".sh": ["bash"], ".bash": ["bash"], ".js": ["node"], ".mjs": ["node"], ".cjs": ["node"]}


def _size(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f} MB"


class SkillToolProvider:
    def __init__(self, skills: SkillStore, documents: "DocumentStore | None", workspace_dir: str | Path) -> None:
        self.skills = skills
        self.documents = documents
        self.workspace_root = Path(workspace_dir)

    # ------------------------------------------------------------------ Liste
    def list_tools(self, ctx: ToolContext) -> list[ToolSpec]:
        active = self.skills.list(enabled_only=True)
        if not active:
            return []
        names = ", ".join(s["name"] for s in active)
        specs = [
            ToolSpec(
                name="use_skill",
                description=(
                    "Lädt die Anleitung eines Skills (SKILL.md) samt Liste seiner Dateien. "
                    f"Nutze das, sobald eine Aufgabe zu einem Skill passt. Verfügbar: {names}."
                ),
                parameters={
                    "type": "object",
                    "properties": {"name": {"type": "string", "description": "Name des Skills"}},
                    "required": ["name"],
                },
                kind="read",
                source="skills",
                title="Skill",
            ),
            ToolSpec(
                name="read_skill_file",
                description=(
                    "Liest eine weitere Datei eines Skills (z. B. references/…, Vorlagen, Skripte). "
                    "Mit `query` nur die passenden Zeilen."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Name des Skills"},
                        "path": {"type": "string", "description": "Pfad innerhalb des Skills, z. B. references/api.md"},
                        "query": {"type": "string", "description": "Optional: Stichwörter für eine gezielte Suche"},
                    },
                    "required": ["name", "path"],
                },
                kind="read",
                source="skills",
                title="Skill-Datei",
            ),
        ]
        if self.skills.scripts_enabled and any(s["scripts"] for s in active):
            specs.append(
                ToolSpec(
                    name="run_skill_script",
                    description=(
                        "Führt ein Skript aus dem scripts/-Ordner eines Skills auf dem Rechner des Nutzers aus "
                        "(.py, .sh, .js oder ausführbare Dateien; Timeout 3 Minuten). Arbeitsverzeichnis ist ein "
                        "eigener Ordner pro Chat, in dem auch die im Chat hochgeladenen Dateien liegen; erzeugte "
                        "Dateien landen dort. Die Ausgabe (stdout/stderr) kommt zurück."
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Name des Skills"},
                            "script": {"type": "string", "description": "Pfad im Skill, z. B. scripts/fill_form.py"},
                            "args": {"type": "array", "items": {"type": "string"}, "description": "Argumente"},
                            "stdin": {"type": "string", "description": "Optional: Eingabe über stdin"},
                        },
                        "required": ["name", "script"],
                    },
                    kind="write",
                    source="skills",
                    title="Skill-Skript",
                )
            )
        return specs

    # ----------------------------------------------------------------- Aufruf
    async def call(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            if name == "use_skill":
                return self._use(args)
            if name == "read_skill_file":
                return self._read(args)
            if name == "run_skill_script":
                return await self._run(args, ctx)
        except SkillError as e:
            return ToolResult(str(e), is_error=True)
        return ToolResult(f"Unbekanntes Tool {name}", is_error=True)

    def _skill(self, args: dict[str, Any]) -> dict[str, Any]:
        wanted = str(args.get("name") or "").strip()
        skill = self.skills.get(wanted) or self.skills.get(wanted.lower().replace(" ", "-"))
        if not skill or not skill["enabled"]:
            available = ", ".join(s["name"] for s in self.skills.list(enabled_only=True)) or "–"
            raise SkillError(f"Skill „{wanted}“ nicht gefunden oder deaktiviert. Verfügbar: {available}")
        return skill

    def _use(self, args: dict[str, Any]) -> ToolResult:
        skill = self._skill(args)
        out = [f"# Skill: {skill['name']}", f"Ordner: {skill['dir']}", "", skill["body"]]
        files = skill["file_list"]
        if files:
            out += ["", "## Dateien in diesem Skill (mit `read_skill_file` lesen)"]
            out += [f"- {f['path']} ({_size(f['size'])}{'' if f['text'] else ', binär'})" for f in files[:200]]
            if any(f["path"].startswith("scripts/") for f in files):
                if self.skills.scripts_enabled:
                    out.append("\nSkripte führst du mit `run_skill_script` aus (Pfad relativ zum Skill).")
                else:
                    out.append(
                        "\nHinweis: Skripte ausführen ist in den Einstellungen deaktiviert. Du kannst sie lesen und "
                        "ihre Logik selbst anwenden oder den Nutzer bitten, sie auszuführen."
                    )
        return ToolResult("\n".join(out))

    def _read(self, args: dict[str, Any]) -> ToolResult:
        skill = self._skill(args)
        rel = str(args.get("path") or "").strip().lstrip("./") or "SKILL.md"
        path, data = self.skills.read_file(skill["name"], rel)
        if not is_text(path, data):
            return ToolResult(f"„{rel}“ ist eine Binärdatei ({_size(len(data))}) und kann nicht als Text gelesen werden.")
        text = data.decode("utf-8", errors="replace")
        query = str(args.get("query") or "").strip()
        if query:
            hits = search_lines(text, query)
            return ToolResult(f"# {skill['name']}/{rel} – Treffer für „{query}“\n{hits}" if hits else f"Keine Treffer für „{query}“ in {rel}.")
        if len(text) > MAX_FILE_CHARS:
            return ToolResult(
                f"# {skill['name']}/{rel}\n{text[:MAX_FILE_CHARS]}\n\n[… gekürzt, {len(text)} Zeichen insgesamt – "
                "nutze `query` für gezielte Stellen]"
            )
        return ToolResult(f"# {skill['name']}/{rel}\n{text}")

    # ---------------------------------------------------------------- Skripte
    def workspace(self, conversation_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", conversation_id) or "chat"
        ws = (self.workspace_root / safe).resolve()
        ws.mkdir(parents=True, exist_ok=True)
        if self.documents:
            for f in self.documents.for_chat(conversation_id):
                src = self.documents.original_path(f["id"])
                dest = ws / Path(f["name"]).name
                if src and not dest.exists():
                    shutil.copy2(src, dest)
        return ws

    async def _run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if not self.skills.scripts_enabled:
            return ToolResult("Skripte ausführen ist in den Einstellungen deaktiviert.", is_error=True)
        skill = self._skill(args)
        rel = str(args.get("script") or "").strip().lstrip("./")
        path, _ = self.skills.read_file(skill["name"], rel)
        cmd_args = [str(a) for a in (args.get("args") or [])]
        interp = INTERPRETERS.get(path.suffix.lower())
        if interp:
            if not shutil.which(interp[0]) and not Path(interp[0]).exists():
                return ToolResult(f"„{interp[0]}“ ist auf diesem Rechner nicht installiert.", is_error=True)
            cmd = [*interp, str(path), *cmd_args]
        elif os.access(path, os.X_OK):
            cmd = [str(path), *cmd_args]
        else:
            return ToolResult(f"Unbekannter Skripttyp „{path.suffix}“ – unterstützt: .py, .sh, .js oder ausführbar.", is_error=True)
        ws = self.workspace(ctx.conversation_id)
        before = {p: p.stat().st_mtime for p in ws.rglob("*") if p.is_file()}
        started = time.time()
        env = {**os.environ, "SKILL_DIR": str(Path(skill["dir"])), "WORKSPACE": str(ws), "PYTHONUNBUFFERED": "1"}
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=ws,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdin = str(args.get("stdin") or "").encode() or None
        try:
            out, _ = await asyncio.wait_for(proc.communicate(stdin), timeout=SCRIPT_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return ToolResult(f"Abgebrochen: Das Skript lief länger als {SCRIPT_TIMEOUT} Sekunden.", is_error=True)
        except asyncio.CancelledError:
            proc.kill()
            raise
        text = out.decode("utf-8", errors="replace")
        if len(text) > MAX_OUTPUT_CHARS:
            text = text[:MAX_OUTPUT_CHARS // 2] + "\n[… Ausgabe gekürzt …]\n" + text[-MAX_OUTPUT_CHARS // 2:]
        changed = sorted(
            p.relative_to(ws).as_posix() for p in ws.rglob("*") if p.is_file() and before.get(p) != p.stat().st_mtime
        )
        summary = [f"Exit-Code {proc.returncode} nach {time.time() - started:.1f} s · Arbeitsordner: {ws}"]
        if changed:
            summary.append("Neue/geänderte Dateien: " + ", ".join(changed[:30]))
        summary.append("\n" + (text.strip() or "(keine Ausgabe)"))
        return ToolResult("\n".join(summary), is_error=proc.returncode != 0)

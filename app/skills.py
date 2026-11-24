"""Skills im offenen Agent-Skills-Format (wie Claude Code, Codex & Co.).

Ein Skill ist ein Ordner mit einer SKILL.md (YAML-Frontmatter mit `name` und
`description`, darunter die Anleitung in Markdown) und optional weiteren Dateien
(references/, scripts/, assets/ …). Progressive Disclosure:

1. Im Systemprompt stehen nur Name + Beschreibung aller aktiven Skills.
2. Passt eine Aufgabe, lädt der Assistent die Anleitung mit `use_skill`.
3. Zusatzdateien liest er bei Bedarf mit `read_skill_file`; Skripte laufen
   (wenn erlaubt) mit `run_skill_script`.

Gespeichert wird unter SKILLS_DIR (Standard ./data/skills/<name>/), aktiv/inaktiv
in der Datenbank.
"""

from __future__ import annotations

import base64
import io
import json
import re
import shutil
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from .memory.store import MemoryStore

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_NAME = 64
MAX_DESCRIPTION = 1024
MAX_ARCHIVE_BYTES = 60 * 1024 * 1024
MAX_ARCHIVE_FILES = 1000
IGNORED_PARTS = {"__MACOSX", ".DS_Store", ".git", "__pycache__", "node_modules", ".venv"}
TEXT_SUFFIXES = {
    ".md", ".txt", ".py", ".sh", ".bash", ".zsh", ".js", ".mjs", ".cjs", ".ts", ".json", ".yaml", ".yml", ".toml",
    ".csv", ".tsv", ".xml", ".html", ".htm", ".css", ".sql", ".ini", ".cfg", ".rst", ".tex", ".xsd", ".jinja", ".j2",
}


class SkillError(ValueError):
    pass


# ------------------------------------------------------------------ SKILL.md
def parse_skill_md(text: str) -> tuple[dict[str, Any], str]:
    """Trennt YAML-Frontmatter und Markdown-Inhalt."""
    text = text.lstrip("﻿")
    m = re.match(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text, re.DOTALL)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise SkillError(f"Frontmatter ist kein gültiges YAML: {e}") from e
    if not isinstance(meta, dict):
        raise SkillError("Frontmatter muss aus Schlüssel-Wert-Paaren bestehen")
    return meta, text[m.end():]


def render_skill_md(meta: dict[str, Any], body: str) -> str:
    ordered = {"name": meta.get("name"), "description": meta.get("description")}
    ordered.update({k: v for k, v in meta.items() if k not in ordered})
    front = yaml.safe_dump(ordered, allow_unicode=True, sort_keys=False, width=1000).strip()
    return f"---\n{front}\n---\n\n{body.strip()}\n"


def slugify(name: str) -> str:
    s = name.strip().lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:MAX_NAME].strip("-") or "skill"


def validate(name: str, description: str) -> None:
    if not NAME_RE.match(name) or len(name) > MAX_NAME:
        raise SkillError("Name: nur Kleinbuchstaben, Ziffern und Bindestriche (max. 64 Zeichen), z. B. „quartalsbericht“")
    if not description.strip():
        raise SkillError("Die Beschreibung fehlt – an ihr erkennt der Assistent, wann der Skill passt")
    if len(description) > MAX_DESCRIPTION:
        raise SkillError(f"Beschreibung zu lang ({len(description)} > {MAX_DESCRIPTION} Zeichen)")


def _safe_rel(path: str) -> PurePosixPath:
    p = PurePosixPath(path.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise SkillError(f"Ungültiger Pfad: {path}")
    return p


def is_text(path: Path | PurePosixPath, data: bytes | None = None) -> bool:
    if path.suffix.lower() in TEXT_SUFFIXES or path.name.lower() in {"license", "readme", "makefile"}:
        return True
    if data is not None:
        try:
            data[:4096].decode("utf-8")
            return b"\x00" not in data[:4096]
        except UnicodeDecodeError:
            return False
    return False


# ------------------------------------------------------------------ Speicher
class SkillStore:
    def __init__(self, root: str | Path, store: MemoryStore) -> None:
        self.root = Path(root)
        self.store = store

    # ----------------------------------------------------------- Aktiv/Inaktiv
    def _disabled(self) -> set[str]:
        try:
            return set(json.loads(self.store.get_meta("skills_disabled", "[]") or "[]"))
        except ValueError:
            return set()

    def set_enabled(self, name: str, enabled: bool) -> None:
        if not self.path(name):
            raise SkillError(f"Skill „{name}“ gibt es nicht")
        off = self._disabled()
        (off.discard if enabled else off.add)(name)
        self.store.set_meta("skills_disabled", json.dumps(sorted(off)))

    @property
    def scripts_enabled(self) -> bool:
        return self.store.get_meta("skills_scripts", "0") == "1"

    def set_scripts_enabled(self, value: bool) -> None:
        self.store.set_meta("skills_scripts", "1" if value else "0")

    # ---------------------------------------------------------------- Lesen
    def path(self, name: str) -> Path | None:
        if not NAME_RE.match(name or ""):
            return None
        p = self.root / name
        return p if (p / "SKILL.md").is_file() else None

    def files(self, name: str) -> list[dict[str, Any]]:
        base = self.path(name)
        if not base:
            return []
        out = []
        for f in sorted(base.rglob("*")):
            rel = f.relative_to(base)
            if not f.is_file() or rel.as_posix() == "SKILL.md" or IGNORED_PARTS & set(rel.parts):
                continue
            out.append({"path": rel.as_posix(), "size": f.stat().st_size, "text": is_text(rel)})
        return out

    def _summary(self, base: Path, disabled: set[str]) -> dict[str, Any]:
        name = base.name
        try:
            meta, body = parse_skill_md((base / "SKILL.md").read_text(encoding="utf-8"))
            error = None
        except (SkillError, UnicodeDecodeError) as e:
            meta, body, error = {}, "", str(e)
        files = self.files(name)
        return {
            "name": name,
            "description": str(meta.get("description") or "").strip(),
            "enabled": name not in disabled and error is None,
            "files": len(files),
            "scripts": sum(1 for f in files if f["path"].startswith("scripts/")),
            "chars": len(body),
            "error": error,
        }

    def list(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        disabled = self._disabled()
        out = [self._summary(d, disabled) for d in sorted(self.root.iterdir()) if (d / "SKILL.md").is_file() and NAME_RE.match(d.name)]
        return [s for s in out if s["enabled"]] if enabled_only else out

    def get(self, name: str) -> dict[str, Any] | None:
        base = self.path(name)
        if not base:
            return None
        raw = (base / "SKILL.md").read_text(encoding="utf-8", errors="replace")
        meta, body = parse_skill_md(raw)
        extra = {k: v for k, v in meta.items() if k not in ("name", "description")}
        return {
            **self._summary(base, self._disabled()),
            "body": body.strip(),
            "meta": extra,
            "file_list": self.files(name),
            "dir": str(base.resolve()),
        }

    def read_file(self, name: str, rel: str) -> tuple[Path, bytes]:
        base = self.path(name)
        if not base:
            raise SkillError(f"Skill „{name}“ gibt es nicht")
        p = (base / _safe_rel(rel)).resolve()
        if not p.is_relative_to(base.resolve()) or not p.is_file():
            raise SkillError(f"Datei „{rel}“ gibt es in „{name}“ nicht")
        return p, p.read_bytes()

    # -------------------------------------------------------------- Schreiben
    def save(self, name: str, description: str, body: str, *, old_name: str | None = None) -> str:
        name = name.strip()
        validate(name, description)
        meta: dict[str, Any] = {}
        if old_name:
            src = self.path(old_name)
            if not src:
                raise SkillError(f"Skill „{old_name}“ gibt es nicht")
            meta, _ = parse_skill_md((src / "SKILL.md").read_text(encoding="utf-8", errors="replace"))
            if name != old_name:
                if self.path(name):
                    raise SkillError(f"Es gibt schon einen Skill „{name}“")
                src.rename(self.root / name)
                off = self._disabled()
                if old_name in off:
                    self.store.set_meta("skills_disabled", json.dumps(sorted(off - {old_name} | {name})))
        elif self.path(name):
            raise SkillError(f"Es gibt schon einen Skill „{name}“")
        meta.update({"name": name, "description": " ".join(description.split())})
        target = self.root / name
        target.mkdir(parents=True, exist_ok=True)
        (target / "SKILL.md").write_text(render_skill_md(meta, body), encoding="utf-8")
        return name

    def delete(self, name: str) -> bool:
        base = self.path(name)
        if not base:
            return False
        shutil.rmtree(base)
        off = self._disabled() - {name}
        self.store.set_meta("skills_disabled", json.dumps(sorted(off)))
        return True

    def unique_name(self, name: str, taken: set[str] | None = None) -> str:
        taken = taken or set()
        i = 2
        while self.path(f"{name}-{i}") or f"{name}-{i}" in taken:
            i += 1
        return f"{name}-{i}"[:MAX_NAME]

    def install(self, files: dict[str, bytes], *, name: str | None = None, on_conflict: str = "rename") -> dict[str, Any]:
        """Legt einen Skill aus {relativer Pfad: Inhalt} an. Ergebnis: {name, action}."""
        if "SKILL.md" not in files:
            raise SkillError("SKILL.md fehlt")
        meta, body = parse_skill_md(files["SKILL.md"].decode("utf-8", errors="replace"))
        original = str(meta.get("name") or name or "skill")
        target = slugify(name or original)
        description = str(meta.get("description") or "").strip()
        if not description:
            # Viele SKILL.md im Netz haben keine Beschreibung – erste Textzeile als Notlösung
            first = next((l.strip("# ").strip() for l in body.splitlines() if l.strip()), "")
            description = first[:300] or target
        validate(target, description[:MAX_DESCRIPTION])
        action = "added"
        if self.path(target):
            if on_conflict == "skip":
                return {"name": target, "action": "skipped"}
            if on_conflict == "overwrite":
                shutil.rmtree(self.root / target)
                action = "replaced"
            else:
                target = self.unique_name(target)
                action = "renamed"
        dest = self.root / target
        dest.mkdir(parents=True, exist_ok=True)
        for rel, data in files.items():
            p = _safe_rel(rel)
            out = dest / p
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
            if p.parts[0] == "scripts" and p.suffix in (".sh", ".py", ".bash", ".js"):
                out.chmod(0o755)
        if meta.get("name") != target or meta.get("description") != description[:MAX_DESCRIPTION]:
            meta.update({"name": target, "description": description[:MAX_DESCRIPTION]})
            (dest / "SKILL.md").write_text(render_skill_md(meta, body), encoding="utf-8")
        return {"name": target, "action": action, "original": original}

    # ------------------------------------------------------- Archive (.zip/.skill)
    @staticmethod
    def read_archive(data: bytes, filename: str) -> list[dict[str, Any]]:
        """Findet Skills in einer .zip/.skill-Datei oder einer einzelnen SKILL.md.
        Ergebnis: [{name (Ordnername oder None), files: {rel: bytes}}]"""
        lower = filename.lower()
        if lower.endswith(".md"):
            return [{"name": None if Path(lower).stem.upper() == "SKILL" else Path(filename).stem, "files": {"SKILL.md": data}}]
        if len(data) > MAX_ARCHIVE_BYTES:
            raise SkillError("Archiv ist zu groß (max. 60 MB)")
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as e:
            raise SkillError("Keine gültige .zip/.skill-Datei") from e
        entries = [i for i in zf.infolist() if not i.is_dir() and not IGNORED_PARTS & set(PurePosixPath(i.filename).parts)]
        if len(entries) > MAX_ARCHIVE_FILES:
            raise SkillError(f"Archiv enthält zu viele Dateien (max. {MAX_ARCHIVE_FILES})")
        if sum(i.file_size for i in entries) > MAX_ARCHIVE_BYTES:
            raise SkillError("Archiv ist entpackt zu groß (max. 60 MB)")
        paths = {PurePosixPath(i.filename.replace("\\", "/")): i for i in entries}
        roots = sorted((p.parent for p in paths if p.name == "SKILL.md"), key=lambda r: len(r.parts))
        # Nur die obersten Skill-Ordner (ein Skill darf Beispiel-SKILL.md in Unterordnern haben)
        top: list[PurePosixPath] = []
        for r in roots:
            if not any(r != t and (t == PurePosixPath(".") or r.is_relative_to(t)) for t in top):
                top.append(r)
        if not top:
            raise SkillError("Im Archiv wurde keine SKILL.md gefunden")
        # Die obersten Skill-Ordner überschneiden sich nicht → jede Datei gehört zu höchstens einem
        skills = []
        for root in top:
            files: dict[str, bytes] = {}
            for p, info in paths.items():
                if p.is_relative_to(root):
                    rel = p.relative_to(root).as_posix()
                    _safe_rel(rel)
                    files[rel] = zf.read(info)
            folder = root.name if root != PurePosixPath(".") else Path(filename).stem
            skills.append({"name": folder or None, "files": files})
        return skills

    def import_archive(self, data: bytes, filename: str, on_conflict: str = "rename") -> list[dict[str, Any]]:
        results = []
        for sk in self.read_archive(data, filename):
            meta, _ = parse_skill_md(sk["files"]["SKILL.md"].decode("utf-8", errors="replace"))
            name = str(meta.get("name") or sk["name"] or "skill")
            results.append(self.install(sk["files"], name=name, on_conflict=on_conflict))
        return results

    def export_zip(self, names: list[str]) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in names:
                base = self.path(name)
                if not base:
                    raise SkillError(f"Skill „{name}“ gibt es nicht")
                for f in sorted(base.rglob("*")):
                    rel = f.relative_to(base)
                    if f.is_file() and not IGNORED_PARTS & set(rel.parts):
                        zf.write(f, f"{name}/{rel.as_posix()}")
        return buf.getvalue()

    # ----------------------------------------------------------- Pakete (JSON)
    def to_bundle(self, name: str) -> dict[str, Any]:
        base = self.path(name)
        if not base:
            raise SkillError(f"Skill „{name}“ gibt es nicht")
        files: dict[str, Any] = {}
        for f in sorted(base.rglob("*")):
            rel = f.relative_to(base)
            if not f.is_file() or IGNORED_PARTS & set(rel.parts):
                continue
            data = f.read_bytes()
            if is_text(rel, data):
                files[rel.as_posix()] = data.decode("utf-8", errors="replace")
            else:
                files[rel.as_posix()] = {"base64": base64.b64encode(data).decode()}
        return {"name": name, "enabled": name not in self._disabled(), "files": files}

    @staticmethod
    def bundle_files(entry: dict[str, Any]) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        for rel, value in (entry.get("files") or {}).items():
            _safe_rel(rel)
            if isinstance(value, str):
                out[rel] = value.encode("utf-8")
            elif isinstance(value, dict) and isinstance(value.get("base64"), str):
                out[rel] = base64.b64decode(value["base64"])
            else:
                raise SkillError(f"Skill „{entry.get('name')}“: ungültiger Eintrag für {rel}")
        if "SKILL.md" not in out:
            raise SkillError(f"Skill „{entry.get('name')}“: SKILL.md fehlt")
        return out

    # ---------------------------------------------------------- Systemprompt
    def prompt_block(self) -> str:
        skills = self.list(enabled_only=True)
        if not skills:
            return ""
        lines = [
            "\n## Skills",
            "Skills sind Anleitungen (teils mit Vorlagen und Skripten) für bestimmte Aufgaben. "
            "Wenn eine Anfrage zu einer Beschreibung passt, lade den Skill mit `use_skill`, bevor du loslegst, "
            "und folge seiner Anleitung. Verweist sie auf weitere Dateien, lies sie mit `read_skill_file`.",
        ]
        for s in skills:
            lines.append(f"- **{s['name']}**: {' '.join(s['description'].split())}")
        return "\n".join(lines) + "\n"

"""Export des Gedächtnisses als Obsidian-kompatibler Markdown-Vault (Einweg-Export).

Jede Entität wird eine Notiz mit [[Wikilinks]] zu verbundenen Entitäten.
Generierte Dateien sind per Frontmatter markiert; nur diese werden beim
nächsten Export überschrieben bzw. entfernt – eigene Notizen bleiben unberührt.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .store import MemoryStore

FOLDERS = {
    "self": "Ich",
    "person": "Personen",
    "project": "Projekte",
    "organization": "Organisationen",
    "idea": "Ideen",
    "strategy": "Strategien",
    "goal": "Ziele",
    "life_area": "Lebensbereiche",
    "topic": "Themen",
}
MARKER = "generated_by: second-brain-harness"


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|#^\[\]]', "-", name).strip(" .")
    return name[:120] or "Unbenannt"


def _yaml_value(v) -> str:
    return json.dumps(v, ensure_ascii=False)


class VaultExporter:
    def __init__(self, store: MemoryStore, export_dir: str) -> None:
        self.store = store
        self.root = Path(export_dir).expanduser() if export_dir else None

    @property
    def enabled(self) -> bool:
        return self.root is not None

    def export(self) -> dict[str, int]:
        if not self.root:
            return {"files": 0}
        self.root.mkdir(parents=True, exist_ok=True)
        written: set[Path] = set()

        entities = self.store.list_entities(limit=100000)
        link_name = {e["id"]: safe_filename(e["name"]) for e in entities}

        for ent in entities:
            folder = self.root / FOLDERS.get(ent["type"], "Themen")
            folder.mkdir(exist_ok=True)
            path = folder / f"{link_name[ent['id']]}.md"
            path.write_text(self._entity_note(ent, link_name), encoding="utf-8")
            written.add(path)

        profile = self.store.get_profile()
        p = self.root / "Profil.md"
        p.write_text(
            f"---\n{MARKER}\naktualisiert: {profile.get('updated_at') or ''}\n---\n# Profil\n\n"
            f"{profile.get('content') or '_Noch kein Profil erstellt._'}\n",
            encoding="utf-8",
        )
        written.add(p)

        conv_dir = self.root / "Gespräche"
        conv_dir.mkdir(exist_ok=True)
        for c in self.store.list_conversations(limit=100000):
            if not c.get("summary"):
                continue
            path = conv_dir / f"{c['created_at'][:10]} {safe_filename(c.get('title') or c['id'])}.md"
            path.write_text(
                f"---\n{MARKER}\nchat_id: {_yaml_value(c['id'])}\ndatum: {c['created_at'][:10]}\n---\n"
                f"# {c.get('title') or 'Gespräch'}\n\n{c['summary']}\n",
                encoding="utf-8",
            )
            written.add(path)

        # Veraltete, von uns generierte Dateien entfernen
        removed = 0
        for path in self.root.rglob("*.md"):
            if path in written:
                continue
            try:
                head = path.read_text(encoding="utf-8")[:300]
            except OSError:
                continue
            if MARKER in head:
                path.unlink()
                removed += 1
        return {"files": len(written), "removed": removed}

    def _entity_note(self, ent: dict, link_name: dict[int, str]) -> str:
        fm = [
            "---",
            MARKER,
            f"id: {ent['id']}",
            f"typ: {ent['type']}",
            f"status: {ent['status']}",
            f"aliases: {_yaml_value(ent['aliases'])}",
        ]
        reserved = {"id", "typ", "status", "aliases", "aktualisiert", "generated_by"}
        for k, v in ent["attributes"].items():
            key = re.sub(r"[^\w-]", "_", str(k))
            if key in reserved:
                key = f"{key}_attr" if key != "status" else "projektstatus"
            fm.append(f"{key}: {_yaml_value(v)}")
        fm.append(f"aktualisiert: {ent['updated_at']}")
        fm.append("---")

        body = [f"# {ent['name']}", ""]
        if ent["summary"]:
            body += [ent["summary"], ""]

        rels = self.store.relations_for_entity(ent["id"])
        if rels:
            body.append("## Beziehungen")
            for r in rels:
                if r["source_id"] == ent["id"]:
                    target = link_name.get(r["target_id"], r["target_name"])
                    line = f"- {r['type']} → [[{target}]]"
                else:
                    source = link_name.get(r["source_id"], r["source_name"])
                    line = f"- [[{source}]] {r['type']} → diese"
                if r["description"]:
                    line += f" ({r['description']})"
                body.append(line)
            body.append("")

        facts = self.store.list_facts(entity_id=ent["id"], include_inactive=True, limit=100000)
        active = [f for f in facts if f["status"] == "active"]
        old = [f for f in facts if f["status"] == "superseded"]
        if active:
            body.append("## Fakten")
            for f in active:
                others = [
                    f"[[{link_name[e['id']]}]]" for e in f["entities"] if e["id"] != ent["id"] and e["id"] in link_name
                ]
                line = f"- {f['content']} _({f['created_at'][:10]}, {f['category']})_"
                if others:
                    line += " · " + ", ".join(others)
                body.append(line)
            body.append("")
        if old:
            body.append("## Historie")
            for f in old:
                body.append(f"- ~~{f['content']}~~ _({f['created_at'][:10]})_")
            body.append("")
        return "\n".join(fm + body)

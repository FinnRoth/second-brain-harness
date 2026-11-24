import asyncio
import io
import zipfile
from pathlib import Path

import pytest
from conftest import FakeLLM, tool_call

from app.skills import SkillError, parse_skill_md

SKILL_MD = """---
name: quartalsbericht
description: Erstellt Quartalsberichte im Firmenformat. Nutzen, wenn ein Quartalsbericht oder Q-Report gebraucht wird.
license: MIT
---

# Quartalsbericht

1. Lies references/aufbau.md
2. Schreibe die Abschnitte Umsatz, Kosten, Ausblick.
"""


def make_zip(entries: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


async def import_zip(client, data: bytes, filename="skills.zip", on_conflict="rename"):
    r = await client.post(
        "/api/skills/import", files={"file": (filename, data, "application/zip")}, data={"on_conflict": on_conflict}
    )
    return r


async def run_chat(services, client, content, mode="ask"):
    cid = (await client.post("/api/chats", json={"permission_mode": mode})).json()["id"]
    r = await client.post(f"/api/chats/{cid}/messages", json={"content": content})
    assert r.status_code == 202, r.text
    await asyncio.wait_for(services.runs.runs[r.json()["run_id"]].task, 20)
    return cid


def tool_results(payload):
    return [m["content"] for m in payload["messages"] if m.get("role") == "tool"]


async def test_create_edit_toggle_delete(client, services):
    r = await client.post("/api/skills", json={"name": "Quartals Bericht", "description": "x", "body": "a"})
    assert r.status_code == 400 and "Kleinbuchstaben" in r.json()["detail"]
    r = await client.post("/api/skills", json={"name": "bericht", "description": "", "body": "a"})
    assert r.status_code == 400

    r = await client.post("/api/skills", json={"name": "bericht", "description": "Schreibt Berichte", "body": "# Anleitung\nKurz."})
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is True
    assert "**bericht**: Schreibt Berichte" in services.prompt_builder.skills.prompt_block()

    # Umbenennen behält weitere Frontmatter-Felder und den Aus-Zustand
    raw = services.skills.root / "bericht" / "SKILL.md"
    raw.write_text(raw.read_text().replace("description:", "license: MIT\ndescription:"))
    await client.put("/api/skills/bericht/enabled", json={"enabled": False})
    r = await client.put("/api/skills/bericht", json={"name": "report", "description": "Neu", "body": "Text"})
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["name"] == "report" and s["meta"] == {"license": "MIT"} and s["enabled"] is False
    assert services.skills.prompt_block() == ""
    assert (await client.get("/api/skills/bericht")).status_code == 404

    await client.put("/api/skills/report/enabled", json={"enabled": True})
    assert "report" in services.skills.prompt_block()
    assert (await client.delete("/api/skills/report")).status_code == 200
    assert (await client.get("/api/skills")).json()["skills"] == []


async def test_import_zip_variants_and_export(client, services):
    # Zwei Skills in Unterordnern + macOS-Müll + Beispiel-SKILL.md in einem Unterordner
    data = make_zip(
        {
            "pack/quartalsbericht/SKILL.md": SKILL_MD,
            "pack/quartalsbericht/references/aufbau.md": "# Aufbau\nUmsatz zuerst.",
            "pack/quartalsbericht/examples/demo/SKILL.md": "---\nname: demo\ndescription: d\n---\nx",
            "pack/quartalsbericht/assets/logo.png": b"\x89PNG\r\n\x1a\n\x00\x00",
            "pack/mail-stil/SKILL.md": "---\nname: mail-stil\ndescription: Formuliert E-Mails im eigenen Stil\n---\nKurz und freundlich.",
            "__MACOSX/pack/._x": b"\x00",
        }
    )
    r = await import_zip(client, data)
    assert r.status_code == 200, r.text
    assert sorted(x["name"] for x in r.json()["imported"]) == ["mail-stil", "quartalsbericht"]
    detail = (await client.get("/api/skills/quartalsbericht")).json()
    paths = {f["path"] for f in detail["file_list"]}
    assert paths == {"references/aufbau.md", "examples/demo/SKILL.md", "assets/logo.png"}

    # .skill mit SKILL.md direkt auf oberster Ebene, Konflikt → umbenannt / übersprungen / ersetzt
    single = make_zip({"SKILL.md": SKILL_MD.replace("Firmenformat", "neuen Format")})
    r = await import_zip(client, single, "quartalsbericht.skill")
    assert r.json()["imported"][0] == {"name": "quartalsbericht-2", "action": "renamed", "original": "quartalsbericht"}
    r = await import_zip(client, single, "quartalsbericht.skill", on_conflict="skip")
    assert r.json()["imported"][0]["action"] == "skipped"
    r = await import_zip(client, single, "quartalsbericht.skill", on_conflict="overwrite")
    assert r.json()["imported"][0]["action"] == "replaced"
    assert "neuen Format" in (await client.get("/api/skills/quartalsbericht")).json()["description"]

    # Einzelne .md ohne Frontmatter → Name aus Dateiname, Beschreibung aus erster Zeile
    r = await client.post("/api/skills/import", files={"file": ("Code Review.md", b"# Reviews gruendlich machen\nSchritte ...")})
    assert r.json()["imported"][0]["name"] == "code-review"
    assert (await client.get("/api/skills/code-review")).json()["description"] == "Reviews gruendlich machen"

    # Export → Re-Import liefert dieselben Dateien
    r = await client.get("/api/skills/export?names=mail-stil,quartalsbericht-2")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "mail-stil/SKILL.md" in names and "quartalsbericht-2/SKILL.md" in names


async def test_import_rejects_bad_archives(client):
    r = await import_zip(client, make_zip({"x/readme.md": "nix"}))
    assert r.status_code == 400 and "SKILL.md" in r.json()["detail"]
    r = await import_zip(client, make_zip({"s/SKILL.md": "---\nname: s\ndescription: d\n---\n", "s/../../evil.txt": "x"}))
    assert r.status_code == 400
    r = await import_zip(client, b"kein zip")
    assert r.status_code == 400
    with pytest.raises(SkillError):
        parse_skill_md("---\nname: [unclosed\n---\n")


async def test_skill_tools_in_chat(client, services, fake_llm: FakeLLM):
    await import_zip(client, make_zip({"q/SKILL.md": SKILL_MD, "q/references/aufbau.md": "# Aufbau\nUmsatz zuerst.\nDann Kosten."}))
    seen = []

    def handler(p):
        seen.append(p)
        if len(seen) == 1:
            return {"content": "", "tool_calls": [tool_call("use_skill", {"name": "quartalsbericht"})]}
        if len(seen) == 2:
            return {"content": "", "tool_calls": [tool_call("read_skill_file", {"name": "quartalsbericht", "path": "references/aufbau.md", "query": "Kosten"}, "call_2")]}
        return {"content": "Fertig."}

    fake_llm.handler = handler
    await run_chat(services, client, "Mach mir den Q3-Bericht")
    system = seen[0]["messages"][0]["content"]
    assert "## Skills" in system and "**quartalsbericht**: Erstellt Quartalsberichte" in system
    assert "Schreibe die Abschnitte" not in system  # nur Beschreibung, nicht der Inhalt
    tools = {t["function"]["name"] for t in seen[0]["tools"]}
    assert {"use_skill", "read_skill_file"} <= tools and "run_skill_script" not in tools
    loaded = tool_results(seen[1])[0]
    assert "Schreibe die Abschnitte" in loaded and "references/aufbau.md" in loaded
    assert "Dann Kosten." in tool_results(seen[2])[1]


async def test_run_skill_script(client, services, fake_llm: FakeLLM):
    script = (
        "import os, sys\n"
        "print('args', sys.argv[1:])\n"
        "print('upload', os.path.exists('notiz.txt'))\n"
        "open('ergebnis.txt', 'w').write('ok')\n"
    )
    await import_zip(client, make_zip({
        "q/SKILL.md": SKILL_MD, "q/scripts/build.py": script, "q/scripts/fail.sh": "echo kaputt >&2; exit 3",
    }))
    up = (await client.post("/api/files", files={"file": ("notiz.txt", b"hallo", "text/plain")})).json()

    seen = []

    def handler(p):
        seen.append(p)
        if len(seen) == 1:
            return {"content": "", "tool_calls": [
                tool_call("run_skill_script", {"name": "quartalsbericht", "script": "scripts/build.py", "args": ["--q", "3"]}),
                tool_call("run_skill_script", {"name": "quartalsbericht", "script": "scripts/fail.sh"}, "call_2"),
            ]}
        return {"content": "Fertig."}

    fake_llm.handler = handler
    # Ohne Freigabe in den Einstellungen gibt es das Tool nicht
    await run_chat(services, client, "los", mode="auto")
    assert "run_skill_script" not in {t["function"]["name"] for t in seen[0]["tools"]}

    await client.put("/api/skills-settings", json={"scripts_enabled": True})
    seen.clear()
    cid = (await client.post("/api/chats", json={"permission_mode": "auto"})).json()["id"]
    r = await client.post(f"/api/chats/{cid}/messages", json={
        "content": "los", "attachments": [{"name": "notiz.txt", "kind": "file", "file_id": up["id"]}]})
    await asyncio.wait_for(services.runs.runs[r.json()["run_id"]].task, 30)
    ok, fail = tool_results(seen[1])
    assert "Exit-Code 0" in ok and "args ['--q', '3']" in ok and "upload True" in ok and "ergebnis.txt" in ok
    assert "Exit-Code 3" in fail and "kaputt" in fail
    assert (Path(services.settings.workspace_dir) / cid / "ergebnis.txt").read_text() == "ok"


async def test_run_script_blocked_in_plan_mode(client, services, fake_llm: FakeLLM):
    await import_zip(client, make_zip({"q/SKILL.md": SKILL_MD, "q/scripts/x.py": "print(1)"}))
    await client.put("/api/skills-settings", json={"scripts_enabled": True})
    fake_llm.handler = lambda p: {"content": "", "tool_calls": [tool_call("run_skill_script", {"name": "quartalsbericht", "script": "scripts/x.py"})]} if len(p["messages"]) < 4 else {"content": "ok"}
    cid = (await client.post("/api/chats", json={"permission_mode": "plan"})).json()["id"]
    r = await client.post(f"/api/chats/{cid}/messages", json={"content": "los"})
    await asyncio.wait_for(services.runs.runs[r.json()["run_id"]].task, 20)
    # Im Plan-Modus wird nichts ausgeführt (der Arbeitsordner entsteht erst beim ersten Lauf)
    assert not (Path(services.settings.workspace_dir) / cid).exists()


async def test_skills_in_bundle(client, services):
    await import_zip(client, make_zip({
        "q/SKILL.md": SKILL_MD, "q/references/aufbau.md": "Aufbau", "q/assets/logo.png": b"\x89PNG\x00\xff",
    }))
    await client.put("/api/skills/quartalsbericht/enabled", json={"enabled": False})
    exp = (await client.get("/api/bundles/exportable")).json()
    assert exp["skills"][0]["name"] == "quartalsbericht" and exp["skills"][0]["files"] == 2
    bundle = (await client.post("/api/bundles/export", json={"skills": ["quartalsbericht"]})).json()
    entry = bundle["skills"][0]
    assert entry["enabled"] is False and entry["files"]["references/aufbau.md"] == "Aufbau"
    assert "base64" in entry["files"]["assets/logo.png"]

    prev = (await client.post("/api/bundles/preview", json=bundle)).json()
    assert prev["skills"][0]["exists"] and prev["skills"][0]["suggested_name"] == "quartalsbericht-2"
    r = await client.post("/api/bundles/import", json={"bundle": bundle, "decisions": {"skills": {"0": {"action": "rename"}}}})
    assert r.status_code == 200, r.text
    assert r.json()["result"]["skills"] == ["quartalsbericht-2"]
    copy = services.skills.root / "quartalsbericht-2"
    assert (copy / "assets" / "logo.png").read_bytes() == b"\x89PNG\x00\xff"
    meta, _ = parse_skill_md((copy / "SKILL.md").read_text())
    assert meta["name"] == "quartalsbericht-2" and meta["license"] == "MIT"
    assert services.skills.get("quartalsbericht-2")["enabled"] is False

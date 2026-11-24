"""Dokumente verstehen: Upload, Konvertierung nach Markdown, Seitenbilder für Vision, Suche.

Pipeline (vergleichbar mit ChatGPT, plus visuelles Verständnis für PDFs):
- PDF:  PyMuPDF4LLM → Markdown pro Seite (Überschriften, Listen, Tabellen). Seiten mit wenig Text
        oder mit Grafiken (Scans, Diagramme, Charts) werden zusätzlich als Bild gerendert.
- DOCX/PPTX/XLSX/XLS/HTML/…: MarkItDown → Markdown (Tabellen, Folien inkl. Notizen, Tabellenblätter).
        PPTX/DOCX optional über LibreOffice → PDF → Seitenbilder (wenn installiert).
- Text/Markdown/CSV/JSON/Code: direkt.
- Bilder: gehen ans Vision-Modell.
Kleine Dokumente kommen komplett in den Kontext, große als Gliederung + Anfang; den Rest liest
der Assistent gezielt über das Tool `read_document`.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .db import Database, utcnow

log = logging.getLogger(__name__)

TEXT_EXT = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".xml", ".log",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".kt", ".go", ".rs", ".rb", ".php", ".c", ".h", ".cpp", ".hpp",
    ".cs", ".swift", ".sh", ".zsh", ".bash", ".sql", ".tex", ".rst", ".org", ".env", ".conf", ".css", ".scss", ".abap",
}
OFFICE_EXT = {".docx", ".pptx", ".xlsx", ".xls", ".html", ".htm", ".epub", ".msg", ".odt", ".odp", ".ods", ".rtf", ".doc", ".ppt"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
NEEDS_SOFFICE = {".doc", ".ppt", ".odt", ".odp", ".ods", ".rtf"}  # erst nach OOXML/PDF konvertieren


def approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


@dataclass
class Converted:
    kind: str
    markdown: str = ""
    sections: list[dict[str, Any]] = field(default_factory=list)  # [{"label": "Seite 3", "text": …}]
    visual_pages: list[int] = field(default_factory=list)  # 1-basierte Seiten, die als Bild helfen
    page_count: int = 0
    pdf_for_render: Path | None = None


# ------------------------------------------------------------------ Konverter
def _soffice() -> str | None:
    for name in ("soffice", "libreoffice", "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        path = shutil.which(name) or (name if Path(name).exists() else None)
        if path:
            return path
    return None


def _soffice_convert(src: Path, target_ext: str, outdir: Path, timeout: int = 120) -> Path | None:
    exe = _soffice()
    if not exe:
        return None
    try:
        subprocess.run(
            [exe, "--headless", "--norestore", "--convert-to", target_ext, "--outdir", str(outdir), str(src)],
            check=True,
            capture_output=True,
            timeout=timeout,
        )
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("LibreOffice-Konvertierung von %s fehlgeschlagen: %s", src.name, e)
        return None
    out = outdir / (src.stem + "." + target_ext)
    return out if out.exists() else None


def _convert_pdf(path: Path) -> Converted:
    import pymupdf
    import pymupdf4llm

    doc = pymupdf.open(path)
    try:
        chunks = pymupdf4llm.to_markdown(doc, page_chunks=True, show_progress=False)
    except TypeError:
        chunks = pymupdf4llm.to_markdown(doc, page_chunks=True)
    sections, visual = [], []
    for i, page in enumerate(doc):
        text = chunks[i]["text"] if i < len(chunks) else page.get_text()
        plain = re.sub(r"\s+", " ", page.get_text()).strip()
        has_images = bool(page.get_images(full=False))
        drawings = len(page.get_drawings()) if len(plain) < 4000 else 0
        # Wenig Text (Scan) oder Grafiken/Diagramme → Bild hilft dem Modell
        if len(plain) < 200 or has_images or drawings > 15:
            visual.append(i + 1)
        sections.append({"label": f"Seite {i + 1}", "text": text.strip()})
    count = doc.page_count
    doc.close()
    md = "\n\n".join(f"<!-- Seite {i + 1} -->\n{s['text']}" for i, s in enumerate(sections))
    return Converted("pdf", md, sections, visual, count, pdf_for_render=path)


_SLIDE_RE = re.compile(r"<!--\s*Slide number:\s*(\d+)\s*-->", re.I)
_SHEET_RE = re.compile(r"^## (.+)$", re.M)


def _convert_office(path: Path, ext: str, workdir: Path) -> Converted:
    from markitdown import MarkItDown

    src = path
    if ext in NEEDS_SOFFICE:
        target = {".doc": "docx", ".odt": "docx", ".rtf": "docx", ".ppt": "pptx", ".odp": "pptx", ".ods": "xlsx"}[ext]
        converted = _soffice_convert(path, target, workdir)
        if not converted:
            raise ValueError(f"{ext}-Dateien brauchen LibreOffice zum Lesen (brew install --cask libreoffice)")
        src, ext = converted, "." + target
    md = MarkItDown(enable_plugins=False).convert_local(str(src)).text_content or ""
    kind = {".docx": "docx", ".pptx": "pptx", ".xlsx": "xlsx", ".xls": "xlsx", ".html": "html", ".htm": "html"}.get(ext, ext[1:])
    sections: list[dict[str, Any]] = []
    if kind == "pptx":
        parts = _SLIDE_RE.split(md)
        for i in range(1, len(parts), 2):
            sections.append({"label": f"Folie {parts[i]}", "text": parts[i + 1].strip()})
        md = "\n\n".join(f"<!-- Folie {s['label'][6:]} -->\n{s['text']}" for s in sections) or md
    elif kind == "xlsx":
        heads = list(_SHEET_RE.finditer(md))
        for j, m in enumerate(heads):
            end = heads[j + 1].start() if j + 1 < len(heads) else len(md)
            sections.append({"label": f"Blatt {m.group(1).strip()}", "text": md[m.start() : end].strip()})
    conv = Converted(kind, md.strip(), sections, page_count=len(sections))
    # Folien/Seiten als Bild – nur mit LibreOffice möglich
    if kind in ("pptx", "docx"):
        pdf = _soffice_convert(src, "pdf", workdir)
        if pdf:
            import pymupdf

            with pymupdf.open(pdf) as d:
                conv.page_count = d.page_count
                if kind == "pptx":
                    conv.visual_pages = list(range(1, d.page_count + 1))
                else:
                    conv.visual_pages = [i + 1 for i, p in enumerate(d) if p.get_images(full=False) or len(p.get_drawings()) > 15]
            conv.pdf_for_render = pdf
    return conv


def convert(path: Path, name: str, workdir: Path) -> Converted:
    ext = Path(name).suffix.lower()
    if ext in IMAGE_EXT:
        return Converted("image")
    if ext == ".pdf":
        return _convert_pdf(path)
    if ext in TEXT_EXT:
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        return Converted("text", text)
    if ext in OFFICE_EXT:
        return _convert_office(path, ext, workdir)
    # Unbekannt: Text versuchen, sonst MarkItDown
    raw = path.read_bytes()[:200_000]
    if b"\x00" not in raw:
        return Converted("text", path.read_text(encoding="utf-8", errors="replace"))
    try:
        return _convert_office(path, ext, workdir)
    except Exception as e:
        raise ValueError(f"Dateityp {ext or '?'} wird nicht unterstützt") from e


def outline(markdown: str, sections: list[dict[str, Any]], limit: int = 60) -> str:
    heads = [l.strip() for l in markdown.splitlines() if re.match(r"^#{1,4}\s+\S", l.strip())][:limit]
    if heads:
        return "\n".join(heads)
    return "\n".join(s["label"] for s in sections[:limit])


# --------------------------------------------------------------------- Speicher
class DocumentStore:
    def __init__(self, db: Database, root: str) -> None:
        self.db = db
        self.root = Path(root).expanduser()

    def dir(self, file_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", file_id):
            raise ValueError("Ungültige Datei-ID")
        return self.root / file_id

    def save_upload(self, name: str, data: bytes, mime: str | None, chat_id: str | None = None) -> dict[str, Any]:
        file_id = uuid.uuid4().hex
        d = self.dir(file_id)
        d.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w.\- ]", "_", Path(name).name)[:180] or "datei"
        original = d / f"original{Path(safe).suffix.lower()}"
        original.write_bytes(data)
        mime = mime or mimetypes.guess_type(safe)[0] or "application/octet-stream"
        now = utcnow()
        self.db.execute(
            "INSERT INTO files (id, chat_id, name, mime, kind, size, status, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (file_id, chat_id, safe, mime, "?", len(data), "processing", now),
        )
        try:
            with tempfile.TemporaryDirectory(dir=d) as work:
                conv = convert(original, safe, Path(work))
                if conv.pdf_for_render and conv.pdf_for_render != original:
                    shutil.copy(conv.pdf_for_render, d / "render.pdf")
            (d / "content.md").write_text(conv.markdown, encoding="utf-8")
            meta = {"sections": [{"label": s["label"], "chars": len(s["text"])} for s in conv.sections]}
            (d / "sections.json").write_text(
                json.dumps([{"label": s["label"], "text": s["text"]} for s in conv.sections], ensure_ascii=False),
                encoding="utf-8",
            )
            self.db.execute(
                "UPDATE files SET kind=?, pages=?, chars=?, visual_pages=?, meta=?, status='ready' WHERE id=?",
                (
                    conv.kind,
                    conv.page_count or None,
                    len(conv.markdown),
                    json.dumps(conv.visual_pages),
                    json.dumps(meta),
                    file_id,
                ),
            )
        except Exception as e:
            log.warning("Datei %s konnte nicht gelesen werden: %s", safe, e)
            self.db.execute("UPDATE files SET status='error', error=? WHERE id=?", (str(e)[:500], file_id))
        return self.get(file_id)

    def get(self, file_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM files WHERE id=?", (file_id,))
        if not row:
            return None
        d = dict(row)
        d["visual_pages"] = json.loads(d.get("visual_pages") or "[]")
        d["meta"] = json.loads(d.get("meta") or "{}")
        d["tokens"] = (d.get("chars") or 0) // 4
        return d

    def attach_to_chat(self, file_ids: list[str], chat_id: str) -> None:
        for fid in file_ids:
            self.db.execute("UPDATE files SET chat_id=? WHERE id=? AND chat_id IS NULL", (chat_id, fid))

    def for_chat(self, chat_id: str) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT id FROM files WHERE chat_id=? AND status='ready' ORDER BY created_at", (chat_id,))
        return [self.get(r["id"]) for r in rows]

    def content(self, file_id: str) -> str:
        p = self.dir(file_id) / "content.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def sections(self, file_id: str) -> list[dict[str, Any]]:
        p = self.dir(file_id) / "sections.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []

    def original_path(self, file_id: str) -> Path | None:
        d = self.dir(file_id)
        hits = sorted(d.glob("original*"))
        return hits[0] if hits else None

    def page_png(self, file_id: str, page: int, dpi: int = 110) -> bytes | None:
        """Rendert Seite/Folie `page` (1-basiert) als PNG (zwischengespeichert)."""
        d = self.dir(file_id)
        cache = d / "pages" / f"{page:04d}.png"
        if cache.exists():
            return cache.read_bytes()
        src = d / "render.pdf"
        if not src.exists():
            orig = self.original_path(file_id)
            if not orig or orig.suffix.lower() != ".pdf":
                return None
            src = orig
        import pymupdf

        with pymupdf.open(src) as doc:
            if not 1 <= page <= doc.page_count:
                return None
            pix = doc[page - 1].get_pixmap(dpi=dpi)
            data = pix.tobytes("png")
        cache.parent.mkdir(exist_ok=True)
        cache.write_bytes(data)
        return data

    def data_url(self, file_id: str) -> str | None:
        f = self.get(file_id)
        p = self.original_path(file_id)
        if not f or not p:
            return None
        return f"data:{f['mime']};base64,{base64.b64encode(p.read_bytes()).decode()}"

    def delete_for_chat(self, chat_id: str) -> None:
        for r in self.db.query("SELECT id FROM files WHERE chat_id=?", (chat_id,)):
            shutil.rmtree(self.dir(r["id"]), ignore_errors=True)
        self.db.execute("DELETE FROM files WHERE chat_id=?", (chat_id,))


# ------------------------------------------------------------ Kontext fürs Modell
def llm_parts_for_file(
    store: DocumentStore,
    file_id: str,
    *,
    inline_tokens: int,
    vision: bool,
    max_page_images: int,
    pdf_native: bool,
) -> list[dict[str, Any]]:
    """Inhalt einer Datei als OpenAI-Content-Parts (Text, ggf. Seitenbilder / natives PDF)."""
    f = store.get(file_id)
    if not f:
        return [{"type": "text", "text": "[Datei nicht mehr vorhanden]"}]
    name = f["name"]
    if f["status"] != "ready":
        return [{"type": "text", "text": f'<datei name="{name}">\n[Konnte nicht gelesen werden: {f.get("error") or "unbekannter Fehler"}]\n</datei>'}]
    if f["kind"] == "image":
        url = store.data_url(file_id)
        return [{"type": "image_url", "image_url": {"url": url}}] if url and vision else [
            {"type": "text", "text": f"[Bild {name} – das Modell kann keine Bilder sehen]"}
        ]
    if f["kind"] == "pdf" and pdf_native:
        url = store.data_url(file_id)
        if url:
            return [{"type": "file", "file": {"filename": name, "file_data": url}}]

    content = store.content(file_id)
    sections = store.sections(file_id)
    unit = {"pdf": "Seiten", "pptx": "Folien", "xlsx": "Tabellenblätter"}.get(f["kind"], "Abschnitte")
    head = f'<datei name="{name}" id="{file_id[:8]}" typ="{f["kind"]}"'
    if f.get("pages"):
        head += f' {unit.lower()}="{f["pages"]}"'
    head += ">"
    if approx_tokens(content) <= inline_tokens:
        body = content or "[leer]"
    else:
        preview = content[: max(4000, inline_tokens)]  # ~1/4 des Budgets als Anfang
        size = f"{approx_tokens(content):,}".replace(",", ".")
        body = (
            f"[Großes Dokument: ~{size} Tokens – nicht vollständig im Kontext. "
            f"Mit dem Tool read_document gezielt nach Stichwörtern suchen oder {unit} lesen.]\n\n"
            f"Gliederung:\n{outline(content, sections) or '–'}\n\nAnfang:\n{preview}\n[…]"
        )
    parts: list[dict[str, Any]] = [{"type": "text", "text": f"{head}\n{body}\n</datei>"}]
    if vision and f["visual_pages"] and max_page_images > 0:
        label = "Folie" if f["kind"] == "pptx" else "Seite"
        for page in f["visual_pages"][:max_page_images]:
            png = store.page_png(file_id, page)
            if png:
                parts.append({"type": "text", "text": f"[{name} – {label} {page} als Bild]"})
                parts.append(
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}}
                )
        rest = len(f["visual_pages"]) - max_page_images
        if rest > 0:
            parts.append({"type": "text", "text": f"[{rest} weitere {label}n mit Grafiken wurden nicht als Bild mitgeschickt]"})
    return parts

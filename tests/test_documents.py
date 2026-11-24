import asyncio
import shutil
import subprocess

import pymupdf
import pytest
from conftest import FakeLLM, tool_call


def make_pdf(path, pages_text=3):
    doc = pymupdf.open()
    for i in range(pages_text):
        p = doc.new_page()
        p.insert_text((72, 72), f"Kapitel {i + 1}", fontsize=18)
        for j in range(25):
            p.insert_text((72, 110 + j * 18), f"Absatz {j}: Der Umsatz in Region {i}-{j} stieg deutlich an.", fontsize=10)
    scan = doc.new_page()  # Seite ohne Text, nur Grafik → Vision
    scan.draw_rect(pymupdf.Rect(72, 72, 500, 400), color=(0, 0, 1), fill=(0.7, 0.8, 1))
    doc.save(path)


def make_xlsx(path):
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Umsatz"
    ws.append(["Region", "Q1", "Q2"])
    ws.append(["Nord", 120, 140])
    ws.append(["Süd", 90, 95])
    wb.create_sheet("Kosten").append(["Posten", "Betrag"])
    wb.save(path)


def make_pptx(path):
    from pptx import Presentation

    prs = Presentation()
    for title, body in [("Strategie 2027", "Wachstum in DACH"), ("Roadmap", "Q1 Pilot, Q2 Rollout")]:
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = title
        s.placeholders[1].text = body
        s.notes_slide.notes_text_frame.text = f"Sprechernotiz zu {title}"
    prs.save(path)


async def upload(client, path, name=None, mime="application/octet-stream"):
    with open(path, "rb") as f:
        r = await client.post("/api/files", files={"file": (name or path.name, f, mime)})
    assert r.status_code == 201, r.text
    return r.json()


async def test_pdf_conversion_sections_and_vision(services, client, tmp_path):
    make_pdf(tmp_path / "bericht.pdf")
    f = await upload(client, tmp_path / "bericht.pdf", mime="application/pdf")
    assert f["status"] == "ready" and f["kind"] == "pdf" and f["pages"] == 4
    assert 4 in f["visual_pages"] and 1 not in f["visual_pages"]
    c = (await client.get(f"/api/files/{f['id']}/content")).json()
    assert "Kapitel 2" in c["content"] and c["sections"][0] == "Seite 1"
    png = await client.get(f"/api/files/{f['id']}/pages/4")
    assert png.status_code == 200 and png.content[:4] == b"\x89PNG"
    assert (await client.get(f"/api/files/{f['id']}/original")).content[:4] == b"%PDF"


async def test_office_formats(services, client, tmp_path):
    make_xlsx(tmp_path / "zahlen.xlsx")
    make_pptx(tmp_path / "deck.pptx")
    x = await upload(client, tmp_path / "zahlen.xlsx")
    assert x["kind"] == "xlsx" and x["pages"] == 2
    content = (await client.get(f"/api/files/{x['id']}/content")).json()
    assert "| Nord | 120 | 140 |" in content["content"] and content["sections"] == ["Blatt Umsatz", "Blatt Kosten"]
    p = await upload(client, tmp_path / "deck.pptx")
    assert p["kind"] == "pptx"
    pc = (await client.get(f"/api/files/{p['id']}/content")).json()
    assert "Strategie 2027" in pc["content"] and "Sprechernotiz zu Roadmap" in pc["content"]
    assert pc["sections"][:2] == ["Folie 1", "Folie 2"]


@pytest.mark.skipif(not shutil.which("soffice"), reason="LibreOffice nicht installiert")
async def test_docx_via_libreoffice_fixture(services, client, tmp_path):
    (tmp_path / "brief.html").write_text("<h1>Vertrag</h1><p>Kündigungsfrist: 3 Monate.</p><table><tr><td>A</td><td>B</td></tr></table>")
    subprocess.run(["soffice", "--headless", "--convert-to", "docx:MS Word 2007 XML", "--outdir", str(tmp_path), str(tmp_path / "brief.html")],
                   check=True, capture_output=True, timeout=120)
    d = await upload(client, tmp_path / "brief.docx")
    assert d["kind"] == "docx" and d["status"] == "ready"
    c = (await client.get(f"/api/files/{d['id']}/content")).json()["content"]
    assert "Vertrag" in c and "Kündigungsfrist" in c


async def test_unsupported_and_limits(services, client, tmp_path):
    (tmp_path / "x.bin").write_bytes(b"\x00\x01\x02" * 100)
    f = await upload(client, tmp_path / "x.bin")
    assert f["status"] == "error"
    services.settings.max_upload_mb = 0
    (tmp_path / "a.txt").write_text("hallo")
    with open(tmp_path / "a.txt", "rb") as fh:
        r = await client.post("/api/files", files={"file": ("a.txt", fh, "text/plain")})
    assert r.status_code == 413


async def test_documents_in_chat_inline_large_and_tool(services, client, fake_llm: FakeLLM, tmp_path, settings):
    settings.doc_inline_tokens = 400  # PDF mit 3 Textseiten ist „groß“
    make_pdf(tmp_path / "bericht.pdf")
    make_xlsx(tmp_path / "zahlen.xlsx")
    pdf = await upload(client, tmp_path / "bericht.pdf", mime="application/pdf")
    xls = await upload(client, tmp_path / "zahlen.xlsx")
    seen = []

    def handler(p):
        seen.append(p)
        if len(seen) == 1:
            return {"content": "", "tool_calls": [tool_call("read_document", {"name": "bericht", "pages": "2"})]}
        return {"content": "Gelesen."}

    fake_llm.handler = handler
    cid = (await client.post("/api/chats", json={})).json()["id"]
    r = await client.post(f"/api/chats/{cid}/messages", json={"content": "Fass zusammen", "attachments": [
        {"name": "bericht.pdf", "kind": "file", "file_id": pdf["id"]},
        {"name": "zahlen.xlsx", "kind": "file", "file_id": xls["id"]}]})
    assert r.status_code == 202, r.text
    await asyncio.wait_for(services.runs.runs[r.json()["run_id"]].task, 15)

    content = seen[0]["messages"][-1]["content"]
    texts = " ".join(p["text"] for p in content if p["type"] == "text")
    images = [p for p in content if p["type"] == "image_url"]
    assert "Großes Dokument" in texts and "Gliederung" in texts  # PDF zu groß → Gliederung + Anfang
    assert "| Nord | 120 | 140 |" in texts  # Excel klein → komplett
    assert len(images) == 1 and images[0]["image_url"]["url"].startswith("data:image/png")  # Seite 4 (Grafik)
    assert "read_document" in {t["function"]["name"] for t in seen[0]["tools"]}
    tool_result = seen[1]["messages"][-1]["content"]
    assert "## Seite 2" in tool_result and "Kapitel 2" in tool_result

    chat = (await client.get(f"/api/chats/{cid}")).json()
    file_parts = [p for p in chat["messages"][0]["parts"] if p["type"] == "file"]
    assert {p["name"] for p in file_parts} == {"bericht.pdf", "zahlen.xlsx"}
    # Chat löschen entfernt die Dateien
    await client.delete(f"/api/chats/{cid}")
    assert (await client.get(f"/api/files/{pdf['id']}")).status_code == 404


async def test_image_upload_goes_to_vision(services, client, fake_llm: FakeLLM, tmp_path):
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 4, 4), False)
    pix.save(tmp_path / "foto.png")
    img = await upload(client, tmp_path / "foto.png", mime="image/png")
    assert img["kind"] == "image"
    fake_llm.handler = lambda p: {"content": "Ein Bild."}
    cid = (await client.post("/api/chats", json={})).json()["id"]
    r = await client.post(f"/api/chats/{cid}/messages", json={"content": "Was ist das?", "attachments": [
        {"name": "foto.png", "kind": "file", "file_id": img["id"]}]})
    await asyncio.wait_for(services.runs.runs[r.json()["run_id"]].task, 10)
    content = fake_llm.calls[0]["messages"][-1]["content"]
    assert any(p["type"] == "image_url" and p["image_url"]["url"].startswith("data:image/png") for p in content)


def test_responses_api_file_part():
    from app.responses_api import messages_to_input

    items = messages_to_input([{"role": "user", "content": [
        {"type": "text", "text": "Lies das"},
        {"type": "file", "file": {"filename": "a.pdf", "file_data": "data:application/pdf;base64,AAA"}}]}])
    assert items[0]["content"][1] == {"type": "input_file", "filename": "a.pdf", "file_data": "data:application/pdf;base64,AAA"}

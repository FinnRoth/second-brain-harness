from pathlib import Path


async def test_serves_app_with_spa_fallback(settings, client):
    web = Path(settings.web_dir)
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<html>APP</html>")
    (web / "assets" / "app.js").write_text("console.log(1)")
    (web / "manifest.webmanifest").write_text("{}")

    r = await client.get("/")
    assert r.text == "<html>APP</html>" and r.headers["cache-control"] == "no-cache"
    r = await client.get("/assets/app.js")
    assert r.text == "console.log(1)" and "immutable" in r.headers["cache-control"]
    assert (await client.get("/manifest.webmanifest")).text == "{}"
    assert (await client.get("/chat/irgendwas")).text == "<html>APP</html>"
    assert (await client.get("/api/gibtsnicht")).status_code == 404
    assert (await client.get("/../../etc/passwd")).text == "<html>APP</html>"
    assert (await client.get("/api/config")).status_code == 200


async def test_hint_when_app_not_built(client):
    r = await client.get("/")
    assert "npm run build" in r.text

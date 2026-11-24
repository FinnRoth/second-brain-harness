"""Einstiegspunkt: uvicorn app.main:app"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .api import mcp_admin, memory_admin, native, openai_compat, skills
from .config import Settings, get_settings
from .llm import LLM
from .services import Services

VERSION = "0.4.0"

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def create_app(settings: Settings | None = None, llm: LLM | None = None, services: Services | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = services or Services(settings or get_settings(), llm=llm)
        app.state.services = svc
        await svc.startup()
        try:
            yield
        finally:
            await svc.shutdown()

    app = FastAPI(title="Second Brain Harness", version=VERSION, lifespan=lifespan)
    if services is not None:  # erlaubt Tests ohne Lifespan
        app.state.services = services
    app.include_router(openai_compat.router)
    app.include_router(memory_admin.router)
    app.include_router(mcp_admin.router)
    app.include_router(native.router)
    app.include_router(skills.router)

    @app.get("/health")
    async def health():
        return {"status": "ok", "app": "second-brain-harness", "version": VERSION}

    _mount_web(app, (services.settings if services else settings or get_settings()).web_dir)
    return app


def _mount_web(app: FastAPI, web_dir: str) -> None:
    """Liefert die gebaute App aus (Single-Page-App: unbekannte Pfade → index.html)."""
    root = Path(web_dir).expanduser().resolve()
    index = root / "index.html"

    @app.get("/{path:path}", include_in_schema=False)
    async def web(path: str):
        if path.split("/", 1)[0] in ("api", "memory", "mcp", "v1", "tools", "approvals"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        if not index.exists():
            return HTMLResponse(
                "<h1>Second Brain Harness</h1><p>Die App ist noch nicht gebaut: "
                "<code>cd web && npm install && npm run build</code> (macht das Install-Skript automatisch).</p>"
                '<p>API-Doku: <a href="/docs">/docs</a></p>',
                status_code=200,
            )
        target = (root / path).resolve()
        if path and target.is_file() and root in target.parents:
            headers = {"Cache-Control": "public, max-age=31536000, immutable"} if path.startswith("assets/") else {
                "Cache-Control": "no-cache"
            }
            return FileResponse(target, headers=headers)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})


app = create_app()

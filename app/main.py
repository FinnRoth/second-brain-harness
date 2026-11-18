"""Einstiegspunkt: uvicorn app.main:app"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api import memory_admin, openai_compat
from .config import Settings, get_settings
from .llm import LLM
from .services import Services

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

    app = FastAPI(title="Second Brain Harness", version="0.1.0", lifespan=lifespan)
    if services is not None:  # erlaubt Tests ohne Lifespan
        app.state.services = services
    app.include_router(openai_compat.router)
    app.include_router(memory_admin.router)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app


app = create_app()

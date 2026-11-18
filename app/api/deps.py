from __future__ import annotations

import secrets
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request

if TYPE_CHECKING:
    from ..services import Services


def get_services(request: Request) -> "Services":
    return request.app.state.services


async def require_api_key(request: Request) -> None:
    key = get_services(request).settings.harness_api_key
    if not key:
        return
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-api-key", "")
    if not token or not secrets.compare_digest(token, key):
        raise HTTPException(status_code=401, detail="Ungültiger oder fehlender API-Key")

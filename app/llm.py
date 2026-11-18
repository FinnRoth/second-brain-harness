"""Client für die (LiteLLM-)Upstream-API im OpenAI-Format."""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Protocol

import httpx

from .config import Settings

log = logging.getLogger(__name__)


class UpstreamError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


class LLM(Protocol):
    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def chat_stream(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]: ...

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]: ...

    async def responses(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def responses_stream(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]: ...

    async def close(self) -> None: ...


def _normalize_base(url: str) -> str:
    url = url.rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]
    return url


class LiteLLMClient:
    def __init__(self, settings: Settings) -> None:
        headers = {"Content-Type": "application/json"}
        if settings.litellm_api_key:
            headers["Authorization"] = f"Bearer {settings.litellm_api_key}"
        self._client = httpx.AsyncClient(
            base_url=_normalize_base(settings.litellm_base_url),
            headers=headers,
            timeout=httpx.Timeout(settings.upstream_timeout, connect=10.0),
        )

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = {**payload, "stream": False}
        try:
            resp = await self._client.post(path, json=body)
        except httpx.HTTPError as e:
            raise UpstreamError(0, f"Verbindung fehlgeschlagen: {e}") from e
        if resp.status_code >= 400:
            raise UpstreamError(resp.status_code, resp.text[:2000])
        return resp.json()

    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post("/v1/chat/completions", payload)

    async def responses(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._post("/v1/responses", payload)

    def chat_stream(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        return self._stream("/v1/chat/completions", payload)

    def responses_stream(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        return self._stream("/v1/responses", payload)

    async def _stream(self, path: str, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """SSE lesen. Funktioniert für Chat Completions (data-only) und Responses (event + data)."""
        body = {**payload, "stream": True}
        try:
            async with self._client.stream("POST", path, json=body) as resp:
                if resp.status_code >= 400:
                    text = (await resp.aread()).decode("utf-8", "replace")
                    raise UpstreamError(resp.status_code, text[:2000])
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        yield json.loads(data)
                    except json.JSONDecodeError:
                        log.warning("Ungültiger SSE-Chunk vom Upstream: %s", data[:200])
        except httpx.HTTPError as e:
            raise UpstreamError(0, f"Verbindung fehlgeschlagen: {e}") from e

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        try:
            resp = await self._client.post("/v1/embeddings", json={"model": model, "input": texts})
        except httpx.HTTPError as e:
            raise UpstreamError(0, f"Verbindung fehlgeschlagen: {e}") from e
        if resp.status_code >= 400:
            raise UpstreamError(resp.status_code, resp.text[:2000])
        data = sorted(resp.json()["data"], key=lambda d: d.get("index", 0))
        return [d["embedding"] for d in data]

    async def close(self) -> None:
        await self._client.aclose()


def message_content(resp: dict[str, Any]) -> str:
    """Text der ersten Antwort aus einer Chat-Completion."""
    try:
        return resp["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError):
        return ""


def parse_json_response(text: str) -> dict[str, Any]:
    """Robustes JSON-Parsing (entfernt ```json-Fences, sucht das äußerste Objekt)."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise

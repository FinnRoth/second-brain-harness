"""Token-Verbrauch, Kosten und Modell-Infos (Kontextfenster, Preise).

Modell-Infos kommen dynamisch von LiteLLM (`/model/info`); für eigene Deployments,
die LiteLLM nicht kennt, lassen sie sich in den Einstellungen überschreiben.
Kosten werden in USD berechnet (wie LiteLLM).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, AsyncIterator

from .db import Database, utcnow

if TYPE_CHECKING:
    from .llm import LLM

log = logging.getLogger(__name__)

REFRESH_SECONDS = 6 * 3600
INFO_FIELDS = ("context_window", "input_cost_per_mtok", "output_cost_per_mtok", "cached_cost_per_mtok")


def normalize_usage(u: dict[str, Any] | None) -> dict[str, int]:
    """OpenAI-/LiteLLM-Usage → einheitliche Zahlen."""
    if not u:
        return {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "reasoning_tokens": 0}
    prompt = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
    completion = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
    cached = int(
        (u.get("prompt_tokens_details") or {}).get("cached_tokens")
        or (u.get("input_tokens_details") or {}).get("cached_tokens")
        or u.get("cache_read_input_tokens")
        or 0
    )
    reasoning = int(
        (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
        or (u.get("output_tokens_details") or {}).get("reasoning_tokens")
        or 0
    )
    return {"prompt_tokens": prompt, "completion_tokens": completion, "cached_tokens": cached, "reasoning_tokens": reasoning}


def summarize_calls(calls: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Mehrere Upstream-Calls (Tool-Loop) zusammenfassen. context_tokens = Größe des letzten Prompts + Antwort."""
    if not calls:
        return None
    norm = [normalize_usage(c) for c in calls]
    total = {k: sum(n[k] for n in norm) for k in norm[0]}
    last = norm[-1]
    total["total_tokens"] = total["prompt_tokens"] + total["completion_tokens"]
    total["context_tokens"] = last["prompt_tokens"] + last["completion_tokens"]
    total["calls"] = len(norm)
    return total


class ModelCatalog:
    def __init__(self, llm: "LLM", db: Database) -> None:
        self.llm = llm
        self.db = db
        self._litellm: dict[str, dict[str, Any]] = {}
        self._fetched_at = 0.0
        self.last_error: str | None = None

    # ------------------------------------------------------------- LiteLLM
    async def refresh(self, force: bool = False) -> None:
        if not force and time.time() - self._fetched_at < REFRESH_SECONDS:
            return
        self._fetched_at = time.time()
        try:
            rows = await self.llm.model_info()
        except Exception as e:  # Modell-Infos sind optional
            self.last_error = str(e)
            log.info("Modell-Infos von LiteLLM nicht verfügbar: %s", e)
            return
        info: dict[str, dict[str, Any]] = {}
        for row in rows or []:
            name = row.get("model_name")
            mi = row.get("model_info") or {}
            if not name:
                continue

            def per_m(v: Any) -> float | None:
                try:
                    return float(v) * 1_000_000 if v is not None else None
                except (TypeError, ValueError):
                    return None

            entry = {
                "context_window": mi.get("max_input_tokens") or mi.get("max_tokens"),
                "input_cost_per_mtok": per_m(mi.get("input_cost_per_token")),
                "output_cost_per_mtok": per_m(mi.get("output_cost_per_token")),
                "cached_cost_per_mtok": per_m(mi.get("cache_read_input_token_cost")),
            }
            # Mehrere Deployments gleichen Namens: erste mit Angaben gewinnt
            if name not in info or not any(info[name].values()):
                info[name] = entry
        self._litellm = info
        self.last_error = None

    # ------------------------------------------------------------ Overrides
    def overrides(self, model: str) -> dict[str, Any]:
        raw = self.db.scalar("SELECT value FROM meta WHERE key=?", (f"model_info:{model}",))
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {}

    def set_overrides(self, model: str, values: dict[str, Any]) -> None:
        clean = {k: values[k] for k in INFO_FIELDS if values.get(k) not in (None, "")}
        if clean:
            self.db.execute(
                "INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"model_info:{model}", json.dumps(clean)),
            )
        else:
            self.db.execute("DELETE FROM meta WHERE key=?", (f"model_info:{model}",))

    def info(self, model: str | None) -> dict[str, Any]:
        model = model or ""
        base = dict(self._litellm.get(model) or {})
        over = self.overrides(model)
        merged = {k: over.get(k, base.get(k)) for k in INFO_FIELDS}
        merged["source"] = {k: ("manuell" if k in over else "litellm" if base.get(k) is not None else None) for k in INFO_FIELDS}
        merged["litellm"] = base
        return merged

    def cost(self, model: str | None, usage: dict[str, Any] | None) -> float | None:
        """Kosten in USD – None, wenn keine Preise bekannt sind."""
        if not usage:
            return None
        info = self.info(model)
        inp, out = info.get("input_cost_per_mtok"), info.get("output_cost_per_mtok")
        if inp is None and out is None:
            return None
        n = normalize_usage(usage) if "cached_tokens" not in usage else usage
        cached = n.get("cached_tokens", 0) or 0
        cached_price = info.get("cached_cost_per_mtok")
        if cached_price is None:
            cached_price = inp
        uncached = max(0, n["prompt_tokens"] - cached)
        total = (uncached * (inp or 0) + cached * (cached_price or 0) + n["completion_tokens"] * (out or 0)) / 1_000_000
        return round(total, 6)


class UsageLog:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(self, kind: str, model: str | None, usage: dict[str, Any] | None, cost: float | None, conversation_id: str | None = None) -> None:
        if not usage:
            return
        n = normalize_usage(usage) if "cached_tokens" not in usage else usage
        if not (n["prompt_tokens"] or n["completion_tokens"]):
            return
        self.db.execute(
            "INSERT INTO usage_log (conversation_id, kind, model, prompt_tokens, completion_tokens, cached_tokens,"
            " reasoning_tokens, cost, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                conversation_id,
                kind,
                model,
                n["prompt_tokens"],
                n["completion_tokens"],
                n.get("cached_tokens", 0),
                n.get("reasoning_tokens", 0),
                cost,
                utcnow(),
            ),
        )

    def summary(self, days: int = 30) -> dict[str, Any]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        cols = (
            "COALESCE(SUM(prompt_tokens),0) AS prompt_tokens, COALESCE(SUM(completion_tokens),0) AS completion_tokens,"
            " COALESCE(SUM(cached_tokens),0) AS cached_tokens, SUM(cost) AS cost, COUNT(*) AS calls,"
            " SUM(CASE WHEN cost IS NULL THEN 1 ELSE 0 END) AS unpriced"
        )
        total = dict(self.db.query_one(f"SELECT {cols} FROM usage_log WHERE created_at>=?", (since,)))
        by_kind = [dict(r) for r in self.db.query(f"SELECT kind, {cols} FROM usage_log WHERE created_at>=? GROUP BY kind", (since,))]
        by_model = [
            dict(r)
            for r in self.db.query(
                f"SELECT model, {cols} FROM usage_log WHERE created_at>=? GROUP BY model ORDER BY prompt_tokens DESC", (since,)
            )
        ]
        by_day = [
            dict(r)
            for r in self.db.query(
                f"SELECT substr(created_at,1,10) AS day, {cols} FROM usage_log WHERE created_at>=? GROUP BY day ORDER BY day",
                (since,),
            )
        ]
        return {"days": days, "total": total, "by_kind": by_kind, "by_model": by_model, "by_day": by_day}


class TrackedLLM:
    """Wrapper um den LLM-Client: protokolliert den Verbrauch von Hintergrundaufgaben
    (Dreaming, Titel, Zusammenfassungen). Chat-Antworten zählt die Session selbst."""

    def __init__(self, inner: "LLM", catalog: ModelCatalog, log_: UsageLog) -> None:
        self.inner = inner
        self.catalog = catalog
        self.usage_log = log_

    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        resp = await self.inner.chat(payload)
        try:
            usage = resp.get("usage") if isinstance(resp, dict) else None
            model = payload.get("model")
            self.usage_log.record("hintergrund", model, usage, self.catalog.cost(model, usage))
        except Exception:
            log.debug("Verbrauch konnte nicht protokolliert werden", exc_info=True)
        return resp

    def chat_stream(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        return self.inner.chat_stream(payload)

    async def responses(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self.inner.responses(payload)

    def responses_stream(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        return self.inner.responses_stream(payload)

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        return await self.inner.embed(model, texts)

    async def model_info(self) -> list[dict[str, Any]]:
        fn = getattr(self.inner, "model_info", None)
        return await fn() if fn else []

    async def close(self) -> None:
        await self.inner.close()

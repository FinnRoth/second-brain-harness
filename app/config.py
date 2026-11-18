"""Zentrale Konfiguration – alles über Umgebungsvariablen bzw. .env steuerbar."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Upstream (LiteLLM) -------------------------------------------------
    litellm_base_url: str = "http://host.docker.internal:4000"
    litellm_api_key: str = ""
    # Kommagetrennte Liste der LiteLLM-Modelle, die in Open WebUI auftauchen sollen
    chat_models: str = "gpt-5"
    # Modell für Hintergrundaufgaben (Extraktion, Profil, Zusammenfassungen).
    # Leer = erstes Modell aus chat_models.
    background_model: str = ""
    # Optionales Embedding-Modell in LiteLLM. Leer = nur Volltextsuche.
    embedding_model: str = ""
    upstream_timeout: float = 600.0

    # --- Harness -------------------------------------------------------------
    # API-Key, den Open WebUI an den Harness schicken muss (leer = keine Auth)
    harness_api_key: str = ""
    user_name: str = "der Nutzer"
    db_path: str = "./data/brain.db"
    timezone: str = "Europe/Berlin"

    # Kontext / Retrieval
    context_token_budget: int = 2500
    max_context_facts: int = 25
    max_detected_entities: int = 6

    # Agent
    max_tool_iterations: int = 6
    show_tool_activity: bool = True
    # Welche Upstream-API für Chats genutzt wird:
    #   chat      → /v1/chat/completions (Standard)
    #   responses → /v1/responses (Tools + Reasoning gleichzeitig, Reasoning-Zusammenfassungen)
    upstream_api: str = "chat"
    # Modelle, die immer über /v1/responses laufen sollen (kommagetrennt)
    responses_models: str = ""
    # Reasoning-Zusammenfassung über die Responses API ("auto", "concise", "detailed", leer = aus)
    responses_reasoning_summary: str = "auto"
    # Was tun, wenn ein Modell auf /v1/chat/completions keine Tools zusammen mit
    # reasoning_effort akzeptiert?
    #   use_responses    → automatisch auf /v1/responses wechseln (Tools + Reasoning)
    #   prefer_reasoning → Gedächtnis-Tools weglassen (automatisches Erinnern/Lernen läuft weiter)
    #   prefer_tools     → reasoning_effort=none setzen
    reasoning_tools_conflict: str = "use_responses"

    # Hintergrund-Jobs
    enable_worker: bool = True
    worker_poll_seconds: float = 2.0
    profile_rebuild_every: int = 8  # nach so vielen Gedächtnis-Änderungen wird das Profil neu gebaut
    summary_idle_minutes: int = 20  # Chat wird nach so viel Inaktivität zusammengefasst
    extraction_max_chars: int = 16000
    json_mode: bool = True  # response_format=json_object für Hintergrundaufgaben

    # Obsidian-/Markdown-Export (leer = deaktiviert)
    export_dir: str = ""

    @property
    def chat_model_list(self) -> list[str]:
        return [m.strip() for m in self.chat_models.split(",") if m.strip()]

    @property
    def responses_model_list(self) -> set[str]:
        return {m.strip() for m in self.responses_models.split(",") if m.strip()}

    @property
    def effective_background_model(self) -> str:
        if self.background_model.strip():
            return self.background_model.strip()
        models = self.chat_model_list
        return models[0] if models else "gpt-5"


@lru_cache
def get_settings() -> Settings:
    return Settings()

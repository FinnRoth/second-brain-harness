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
    max_tool_iterations: int = 8
    # Frühere Tool-Aufrufe (+ Überlegung davor) im Verlauf mitschicken, damit der Assistent
    # später erklären kann, was er warum getan hat. Ergebnisse werden dafür gekürzt.
    history_tool_details: bool = True
    history_tool_result_chars: int = 1500
    # Freigabe-Modus für Tools (plan | ask | auto), pro Chat per /plan, /ask, /auto änderbar
    permission_mode: str = "ask"
    # Ab so vielen Tools bekommt das Modell statt aller Schemas eine Tool-Suche
    tool_search_threshold: int = 40

    # MCP-Server (Format wie Claude Code: {"mcpServers": {...}})
    mcp_config: str = "./config/mcp.json"
    mcp_secrets: str = "./config/secrets.env"
    mcp_log_dir: str = "./data/logs/mcp"
    mcp_startup_wait: float = 20.0  # beim Start so lange auf Server warten (danach laufen sie im Hintergrund an)
    mcp_connect_timeout: float = 120.0  # erster Start per npx/uvx kann dauern (Download)
    mcp_max_output_chars: int = 20000
    mcp_watch_interval: float = 5.0  # Config-Änderungen werden automatisch übernommen (0 = aus)
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

    # Prompt-Aufbau (ChatGPT-Stil)
    saved_memories_token_budget: int = 1500
    recent_chats_count: int = 25
    recent_chats_token_budget: int = 1500
    summary_max_words: int = 500

    # Hintergrund-Jobs / Dreaming
    enable_worker: bool = True
    worker_poll_seconds: float = 2.0
    profile_rebuild_every: int = 8  # nach so vielen Gedächtnis-Änderungen wird die Zusammenfassung neu gebaut
    dream_idle_minutes: int = 15  # Chat wird nach so viel Inaktivität verarbeitet („Dreaming“)
    consolidation_min_facts: int = 8  # ab so vielen Fakten wird eine Entität aufgeräumt
    consolidation_growth: int = 5  # … und erneut, wenn seitdem so viele dazugekommen sind
    extraction_max_chars: int = 16000
    json_mode: bool = True  # response_format=json_object für Hintergrundaufgaben

    # Dokumente
    files_dir: str = "./data/files"
    max_upload_mb: int = 50
    doc_inline_tokens: int = 15000  # bis zu dieser Größe kommt ein Dokument komplett in den Kontext
    doc_vision: bool = True  # Seiten mit Grafiken/Scans zusätzlich als Bild mitschicken (Modell muss Bilder können)
    doc_max_page_images: int = 6  # max. Seitenbilder pro Dokument
    pdf_native: bool = False  # PDFs direkt als Datei ans Modell (nur wenn Anbieter/LiteLLM das unterstützen)

    # Gebaute Web-App (web/dist), wird unter / ausgeliefert
    web_dir: str = "./web/dist"

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

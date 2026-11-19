# Architektur

## Überblick

```
Open WebUI ──(OpenAI-API)──▶ app/api/openai_compat.py
                                   │
                             ChatSession (app/agent/session.py)
             ┌─────────────────────┼─────────────────────────────┐
     PromptBuilder            ToolRegistry                  Upstream
  (app/memory/context.py)   (app/tools/registry.py)     LiteLLM: Chat Completions
     │                        │            │             oder Responses API
  Gedächtnis (SQLite)   MemoryTools    MCPToolProvider    (app/responses_api.py)
                                           │
                                       MCPManager ── ServerConnection × n
                                       (app/mcp)      stdio / HTTP / SSE
             Worker (app/jobs.py): Dreaming, Konsolidierung, Zusammenfassung, Export
```

Alles läuft in **einem Prozess**: API, Hintergrund-Worker, MCP-Host und SQLite. Für den Betrieb ist der Harness als nativer Dienst auf dem Mac (LaunchAgent) vorgesehen. Dadurch laufen MCP-Server direkt auf dem Mac und kommen an Dateien und Apps heran.

## Gedächtnis

### Prompt-Aufbau (ChatGPT-Stil, `app/memory/context.py`)

```
system   Persona + Datum (+ wann das letzte Gespräch war)
         ## Memory-Zusammenfassung   ← Dreaming (inkl. Antwort-Präferenzen mit Sicherheitsangabe)
         ## Gespeicherte Erinnerungen ← gepinnte Fakten, Token-Budget, Rest "im Hintergrund"
         ## Letzte Gespräche          ← ~25 Chats: Datum, Titel, nur Nutzernachrichten
         ## Verbundene Tools (MCP)    ← Server + deren Instructions
…Verlauf…
user     <kontext> Uhrzeit · Freigabe-Modus · Steckbriefe erwähnter Entitäten ·
                   relevante Fakten · ggf. Ergebnisse freigegebener Aktionen </kontext>
         + eigentliche Nachricht
```

- Der Systemprompt ändert sich nur, wenn sich das Gedächtnis ändert. Dadurch bleibt das Prompt-Caching des Anbieters gültig.
- Alles, was sich pro Nachricht ändert, steht im `<kontext>`-Block direkt vor der Nachricht.

### Datenmodell (`app/db.py`, Migrationen v1 + v2)

- **entities**: self, person, project, organization, idea, strategy, goal, life_area, topic. Dazu Aliase, Zusammenfassung, Attribute und Status.
- **facts**: atomare Aussagen mit Kategorie, Wichtigkeit, Quelle und Embedding.
  - `pinned` markiert eine gespeicherte Erinnerung.
  - Veraltete Fakten werden nicht gelöscht, sondern **abgelöst** (`superseded_by`), damit die Historie erhalten bleibt.
- **fact_entities**, **relations** bilden einen schlanken Wissensgraphen.
- **conversations / turns** enthalten die Chats im Wortlaut, Titel, Zusammenfassung, Freigabe-Modus und `dreamed_at`.
- **profile** enthält die Memory-Zusammenfassung.
- **approvals** enthält Tool-Aufrufe, die auf eine Freigabe warten.
- **jobs** ist eine persistente Queue mit Retry, Backoff und Deduplizierung.
- **meta** ist ein Schlüssel-Wert-Speicher (z. B. für den Konsolidierungsstand).
- FTS5 mit Trigram-Index über Fakten und Turns.

### Lesepfad (`app/memory/retrieval.py`)

1. Namen und Aliase bekannter Entitäten in der Nachricht erkennen. Dazu ihren Steckbrief mit Beziehungen laden.
2. Hybrid-Suche: BM25 und optional Embeddings, ergänzt um die Fakten der erkannten Entitäten.
3. Die Treffer per Reciprocal Rank Fusion zusammenführen und nach Wichtigkeit und Aktualität gewichten. Gespeicherte Erinnerungen werden ausgelassen, weil sie schon im Systemprompt stehen.

### Schreibpfad: Dreaming (`app/memory/extraction.py`, `consolidation.py`, `jobs.py`)

1. Jede Nachricht verschiebt den Job `dream_conversation` des Chats nach hinten. Er läuft also erst nach `DREAM_IDLE_MINUTES` Ruhe. Startet ein neuer Chat, werden wartende Jobs vorgezogen.
2. Die **Extraktion** verarbeitet alle neuen Turns eines Chats in einem LLM-Call (bei langen Chats in Blöcken). Bestehende Entitäten und Fakten werden mitgegeben. Das Modell antwortet mit Operationen `add` / `supersede` / `delete` sowie Entitäten und Relationen, und ein Schutz verhindert Duplikate.
3. Dabei entstehen ein **Titel und eine Zusammenfassung** des Chats.
4. **Konsolidierung**: Entitäten mit vielen bzw. stark gewachsenen Fakten werden aufgeräumt. Duplikate werden zusammengeführt, Überholtes gelöscht und der Steckbrief neu geschrieben. Gespeicherte Erinnerungen werden dabei nie gelöscht.
5. Nach N Änderungen wird die **Memory-Zusammenfassung** neu gebaut. Danach folgen Embeddings und der Vault-Export.

## Tools

### ToolRegistry (`app/tools/`)

- Provider liefern Tools vom Typ `ToolSpec` (Name, Schema, `kind` = read / write / memory, Quelle, Policy). Aktuell gibt es zwei: `MemoryToolProvider` und `MCPToolProvider`.
- Der Freigabe-Modus entscheidet pro Tool (`permissions.decide`):

  | kind \ Modus | plan | ask | auto |
  |---|---|---|---|
  | read | allow | allow | allow |
  | write | hidden | **ask** | allow |
  | memory | allow | allow | allow (Inkognito: hidden) |

  Config-Regeln gehen vor: `deny` → hidden, `ask` → ask, `allow` → allow (außer in plan).
- **Tool-Suche**: Gibt es mehr Tools als `TOOL_SEARCH_THRESHOLD`, werden MCP-Tools nur über `tool_search` angeboten. Gefundene Tools werden für den Rest der Antwort freigeschaltet. Der Zustand dafür liegt pro Anfrage im `ToolContext`.

### Freigaben (ask-Modus)

1. Das Modell ruft ein veränderndes Tool auf. Der Harness speichert den Aufruf in `approvals`, führt ihn aber **nicht** aus. Das Modell bekommt als Ergebnis die Anweisung, um Bestätigung zu bitten. In Open WebUI erscheint "⏸️ Wartet auf Freigabe".
2. Bei der nächsten Nachricht im selben Chat:
   - Eine kurze Zustimmung ("ok", "ja", "mach") führt alle vorgemerkten Aktionen aus. Die Ergebnisse landen im `<kontext>`.
   - Eine Ablehnung ("nein", "stopp") verwirft die Aktionen.
   - Bei jeder anderen Nachricht verfallen sie.

   Das Modell kann sich also nicht selbst freigeben.
3. Mit einer eigenen UI können dieselben Freigaben später über Buttons laufen. Die Datenbasis dafür steht schon.

### MCP-Host (`app/mcp/`)

- **config.py** liest Configs im Claude-Code-Format (`mcpServers`).
  - Transporte: `stdio` (command/args/env/cwd), `http` (Streamable HTTP) und `sse`.
  - `${VAR}` bzw. `${VAR:-default}` werden aus der Umgebung und `secrets.env` ersetzt.
  - Erweiterungen: `disabled`, `timeout`, `tools.allow/ask/deny/readOnly`.
- **connection.py** startet pro Server **einen Owner-Task**. Dieser öffnet und schließt Transport und Session (MCP-SDK 2.x `Client`) im selben Task, wie es anyio verlangt.
  - stderr geht in `data/logs/mcp/<name>.log`.
  - Gesundheitscheck alle 30 s über `tools/list` (`ping` gibt es im Protokoll 2026-07-28 nicht mehr). Schlägt ein Aufruf fehl, wird sofort geprüft.
  - Fällt der Server aus, startet er mit exponentiellem Backoff neu.
  - Auf `notifications/tools/list_changed` wird die Tool-Liste aktualisiert.
- **manager.py** startet alle Server parallel. Beim Harness-Start wartet er höchstens `MCP_STARTUP_WAIT`, bis jeder Server einen ersten Startversuch hinter sich hat. Danach geht es im Hintergrund weiter.
  - Hot-Reload bei Dateiänderungen. Nur Server mit geänderter Startkonfiguration werden neu gestartet, Regeln und Timeouts gelten ohne Neustart.
- **Tool-Namen** haben die Form `mcp__<server>__<tool>`, auf 64 Zeichen begrenzt.
- **Ergebnisse:**
  - Text und `structuredContent` werden übernommen, Ressourcen als Text.
  - Bilder erscheinen vorerst nur als Platzhalter.
  - Lange Ausgaben werden gekürzt (`MCP_MAX_OUTPUT_CHARS`).

## Upstream (`app/llm.py`, `app/responses_api.py`)

- Chat Completions ist der Standard. Die Responses API wird verwendet bei `UPSTREAM_API=responses`, für Modelle in `RESPONSES_MODELS` oder automatisch, wenn ein Modell Tools zusammen mit `reasoning_effort` ablehnt.
- **Übersetzung Chat Completions ⇄ Responses API:**
  - Nachrichten werden zu Input-Items, Tool-Calls zu `function_call` / `function_call_output`.
  - Stream-Events werden zurück in Chat-Chunks übersetzt. Reasoning-Zusammenfassungen kommen als `reasoning_content`.
  - Verschlüsseltes Reasoning wird innerhalb des Tool-Loops zurückgereicht. Es gilt immer `store: false`.
- **Fallbacks:**
  - Klappt die Responses API nicht, geht es über Chat Completions ohne Tools weiter.
  - Lehnt der Anbieter Reasoning-Zusammenfassungen oder verschlüsseltes Reasoning ab, werden diese Features abgeschaltet.

## Open-WebUI-Eigenheiten (`app/openwebui.py`)

- Die Chat-ID kommt über `X-OpenWebUI-Chat-Id`, sobald Open WebUI mit `ENABLE_FORWARD_USER_INFO_HEADERS=true` läuft. Ohne den Header dient ein Hash der ersten Nachricht als Ersatz.
- Hintergrundaufgaben (Titel, Tags; erkennbar an `### Task:`) werden ohne Gedächtnis durchgereicht.
- Aktivitätsblöcke (`<details type="memory">`) werden aus der Historie entfernt, bevor sie ans Modell gehen.

## Nächste Schritte

- **Eigene UI:** Eine eigene API mit Chats im Harness (als Baum) und typisierten Events (`message.delta`, `tool.started`, `approval.required` …). Darauf eine Oberfläche mit Chat, Memory-Verwaltung, MCP-Status und Freigabe-Buttons. Der OpenAI-Endpunkt bleibt als Adapter für Open WebUI.
- Bilder aus MCP-Ergebnissen über die Responses API weiterreichen.
- OAuth für Remote-MCP-Server.
- MCP-Prompts als Slash-Befehle, MCP-Ressourcen per @-Erwähnung.

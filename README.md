# Second Brain Harness

Ein Backend für einen persönlichen KI-Assistenten mit **Langzeitgedächtnis im ChatGPT-Stil** und **MCP-Tools wie in Claude Code**. Es ist an deine **eigene LiteLLM-API** angebunden, und alle Daten bleiben lokal.

```
Open WebUI (Docker) ──(OpenAI-API)──▶ Harness (nativ auf dem Mac) ──▶ LiteLLM ──▶ GPT / Claude / …
                                         │
                       ┌─────────────────┼──────────────────────┐
                 Gedächtnis (SQLite)   MCP-Server             Obsidian-Vault
                 Zusammenfassung,      npx / uvx / lokal      (Markdown-Export)
                 Erinnerungen, Chats   oder remote (HTTP)
```

- **Erinnert sich wie ChatGPT:** In jedem Chat stehen eine Memory-Zusammenfassung über dich (inkl. Antwort-Präferenzen), gespeicherte Erinnerungen und deine letzten Gespräche. Dazu kommen pro Nachricht Steckbriefe erwähnter Personen und Projekte.
- **Lernt im Hintergrund ("Dreaming"):** Nach ein paar Minuten Ruhe wird ein Chat als Ganzes verarbeitet und das Wissen aufgeräumt.
- **Ist strukturiert:** Personen, Projekte (Arbeit/privat), Organisationen, Ideen, Strategien und Ziele mit Beziehungen und Historie.
- **Bindet MCP-Server an wie Claude Code:** Server werden einfach per Befehl gestartet (`npx …`, `uvx --from git+…`), ohne Repo-Klonen und ohne URL-Freigabe. Remote-Server laufen über HTTP.
- **Hat Freigabe-Modi** wie Claude Code: `plan`, `ask` und `auto`.

Der Aufbau ist in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) beschrieben.

---

## Installation auf dem Mac

Voraussetzungen: [uv](https://docs.astral.sh/uv/) (`brew install uv`), für Node-basierte MCP-Server `node` (`brew install node`) und Docker Desktop für Open WebUI.

```bash
scripts/macos/install.sh
```

Das Skript:

- legt `.env`, `config/mcp.json` und `config/secrets.env` an (falls sie fehlen),
- installiert die Abhängigkeiten,
- richtet den Harness als **Hintergrunddienst** (LaunchAgent) ein. Er startet beim Login automatisch und nach einem Absturz neu.

Danach `.env` ausfüllen: mindestens `LITELLM_BASE_URL`, `LITELLM_API_KEY`, `CHAT_MODELS`, `HARNESS_API_KEY` und `USER_NAME`. Anschließend den Dienst neu starten:

```bash
launchctl kickstart -k gui/$(id -u)/com.secondbrain.harness
```

Dann Open WebUI starten (der `HARNESS_API_KEY` wird aus `.env` übernommen):

```bash
docker compose up -d
```

- Open WebUI: http://localhost:3000
- Harness-Admin-API (Swagger): http://localhost:8100/docs
- Logs: `~/Library/Logs/SecondBrain/harness.log`

In Open WebUI erscheinen zwei Modelle:

| Modell | Verhalten |
|---|---|
| `brain/<modell>` | Normaler Chat mit Gedächtnis |
| `incognito/<modell>` | Liest das Gedächtnis, speichert aber **nichts** |

Empfehlung für Open WebUI: Die eingebaute Memory-Funktion ausgeschaltet lassen. Function Calling kann auf "Default" bleiben, weil die Tools im Harness laufen.

<details>
<summary>Alternativen: ohne LaunchAgent / komplett in Docker</summary>

Manuell im Terminal starten:

```bash
uv run uvicorn app.main:app --port 8100
```

Komplett in Docker: MCP-Server laufen dann im Container und kommen nicht an Mac-Apps heran. Ordner müssen gemountet werden.

```bash
HARNESS_URL=http://harness:8000/v1 docker compose --profile docker up -d --build
```

</details>

---

## Gedächtnis

So ist der Kontext in jedem Chat aufgebaut, ähnlich wie bei ChatGPT:

| Teil | Inhalt | Pflege |
|---|---|---|
| **Memory-Zusammenfassung** | Person, Arbeit, Projekte, Ziele, Personen, Strategien, **Antwort-Präferenzen** | automatisch (Dreaming) |
| **Gespeicherte Erinnerungen** | Was du ausdrücklich gemerkt haben willst ("Merk dir …") | `remember`-Tool, Admin-API |
| **Letzte Gespräche** | Die letzten ~25 Chats mit Titel und deinen Nachrichten (nicht den Antworten) | automatisch |
| **Kontext pro Nachricht** | Uhrzeit, Steckbriefe erwähnter Personen und Projekte, passende Fakten | Suche pro Nachricht |

Die ersten drei Teile ändern sich nur, wenn sich das Gedächtnis ändert. Dadurch greift das **Prompt-Caching** deines Anbieters, und Antworten werden schneller und günstiger.

**Dreaming:** Nach `DREAM_IDLE_MINUTES` (Standard 15) ohne neue Nachricht wird ein Chat verarbeitet. Dabei werden Fakten, Personen, Projekte und Präferenzen gelernt, ein Titel und eine Zusammenfassung erstellt, und anschließend werden Entitäten mit vielen Fakten aufgeräumt (Duplikate zusammengeführt, Überholtes entfernt). Startest du einen neuen Chat, werden wartende Chats sofort verarbeitet.

Im Chat funktioniert zum Beispiel:

- "Merk dir, dass …"
- "Vergiss das mit …"
- "Was weißt du über Max?"
- "Was hatten wir letzte Woche zu Projekt X besprochen?"

### Ansehen und pflegen

Alle Endpunkte findest du unter http://localhost:8100/docs. Die wichtigsten:

| Endpunkt | Zweck |
|---|---|
| `GET /memory/prompt-preview?message=…` | **Was sieht der Assistent?** Systemprompt plus Kontext zu einer Nachricht |
| `GET /memory/summary` · `POST /memory/profile/rebuild` | Memory-Zusammenfassung ansehen / neu bauen |
| `GET /memory/saved` · `POST /memory/facts/{id}/pin` | Gespeicherte Erinnerungen |
| `GET /memory/entities?type=person` · `GET /memory/entities/{id}` | Personen, Projekte … mit Fakten und Historie |
| `PATCH/DELETE /memory/facts/{id}` | Korrigieren, löschen |
| `POST /memory/entities/{keep}/merge/{dup}` | Duplikate zusammenführen |
| `POST /memory/conversations/{id}/dream` | Chat sofort verarbeiten |

Ist `EXPORT_DIR` gesetzt, wird das Gedächtnis automatisch als **Obsidian-Vault** exportiert: eine Notiz pro Entität mit `[[Links]]`, Profil und Chat-Zusammenfassungen. Der Export geht nur in eine Richtung.

---

## MCP-Server

MCP-Server kommen in `config/mcp.json` und stehen dort im **selben Format wie in Claude Code** bzw. Claude Desktop. Bestehende Configs lassen sich also direkt übernehmen. Beispiele stehen in [`config/mcp.example.json`](config/mcp.example.json):

```jsonc
{
  "mcpServers": {
    // npm-Paket – wird beim ersten Start automatisch geladen
    "github": { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"],
                "env": { "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}" } },
    // Python-Server direkt aus einem Git-Repo – kein Klonen nötig
    "mein-tool": { "command": "uvx", "args": ["--from", "git+https://github.com/ich/mein-mcp", "mein-mcp"] },
    // Node-Server direkt aus GitHub
    "node-tool": { "command": "npx", "args": ["-y", "github:ich/node-mcp"] },
    // lokaler Ordner auf dem Mac
    "obsidian": { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "~/Documents/Obsidian"],
                  "tools": { "ask": ["write_file", "edit_file"] } },
    // Remote-Server
    "linear": { "type": "http", "url": "https://mcp.linear.app/mcp",
                "headers": { "Authorization": "Bearer ${LINEAR_TOKEN}" } }
  }
}
```

- **Secrets** stehen in `config/secrets.env` (`GITHUB_TOKEN=…`) und werden als `${NAME}` eingesetzt. Sie landen nie in der Datenbank und nie im Log.
- **Änderungen** an `mcp.json` oder `secrets.env` werden automatisch übernommen. Nur geänderte Server starten neu.
- **Optionale Felder pro Server:**
  - `"disabled": true`
  - `"timeout": 60`
  - `"tools": {"allow": [], "ask": [], "deny": [], "readOnly": []}`. Hier sind Muster wie `"write_*"` erlaubt.
- **Status und Fehler:**
  - `GET /mcp/servers` zeigt alle Server mit Status.
  - `GET /mcp/servers/{name}/logs` zeigt die stderr-Ausgabe eines Servers.
  - `POST /mcp/servers/{name}/tools/{tool}` ruft ein Tool direkt auf, zum Testen.
- **Viele Tools:** Ab `TOOL_SEARCH_THRESHOLD` (Standard 40) bekommt das Modell eine **Tool-Suche** und schaltet passende Tools selbst frei.

### Freigabe-Modi

| Modus | Lesende Tools | Verändernde Tools |
|---|---|---|
| `plan` | ✅ sofort | ❌ nicht verfügbar. Der Assistent plant nur. |
| `ask` (Standard) | ✅ sofort | ⏸️ wird vorgemerkt. Du antwortest **"ok"** zum Ausführen oder **"nein"** zum Verwerfen. |
| `auto` | ✅ sofort | ✅ sofort |

- **Modus wechseln:** Schreib `/plan`, `/ask` oder `/auto` an den Anfang einer Nachricht. Der Modus gilt dann für diesen Chat, zum Beispiel `/plan Wie strukturiere ich meinen Vault um?`.
- **Was als lesend gilt:** Ein Tool gilt als lesend, wenn der Server es so markiert (`readOnlyHint`) oder du es in `"readOnly"` einträgst.
- **Regeln aus der Config gehen immer vor:** `deny` blendet ein Tool aus, `ask` fragt immer nach, `allow` erlaubt ohne Rückfrage.
- **Gedächtnis-Tools** sind in allen Modi erlaubt.

---

## Reasoning-Modelle und Responses API

Manche Modelle (z. B. neuere GPT-Modelle über Azure) lehnen Tools zusammen mit `reasoning_effort` auf `/v1/chat/completions` ab. Der Harness wechselt dann **automatisch** auf `/v1/responses`. Dort gehen Tools und Reasoning gleichzeitig, und eine Zusammenfassung des Reasonings erscheint in Open WebUI als Denkprozess. Es wird nichts beim Anbieter gespeichert (`store: false`).

Einstellungen dafür: `UPSTREAM_API=responses` (immer), `RESPONSES_MODELS=…` (nur bestimmte Modelle) und `REASONING_TOOLS_CONFLICT` (Verhalten im Konfliktfall).

## Embeddings (optional)

Ohne Embeddings findet der Harness Erinnerungen per Volltextsuche (auch Wortteile) und über die Namenserkennung. Mit `EMBEDDING_MODEL=<LiteLLM-Modell>` kommt eine semantische Suche dazu. Nach einem Wechsel des Modells:

```bash
curl -X POST -H "Authorization: Bearer $HARNESS_API_KEY" http://localhost:8100/memory/reembed
```

## Backup

Das gesamte Gedächtnis steckt in `data/brain.db`:

```bash
sqlite3 data/brain.db ".backup data/brain-$(date +%F).db"
```

## Entwicklung

```bash
uv sync
```

```bash
uv run pytest
```

Die Prompts liegen in `app/prompts/*.md` und lassen sich über `PROMPTS_DIR` durch eigene Versionen ersetzen.

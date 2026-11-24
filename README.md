# Second Brain

Ein persönlicher KI-Assistent als **Mac-App** mit **Langzeitgedächtnis im ChatGPT-Stil** und **MCP-Tools wie in Claude Code**. Er ist an deine **eigene LiteLLM-API** angebunden, und alle Daten bleiben lokal.

```
Second-Brain-App (Fenster/Dock) ──▶ Harness (Hintergrunddienst auf dem Mac) ──▶ LiteLLM ──▶ GPT / Claude / …
                                         │
                       ┌─────────────────┼──────────────────────┐
                 Gedächtnis (SQLite)   MCP-Server             Obsidian-Vault
                 Zusammenfassung,      npx / uvx / lokal      (Markdown-Export)
                 Erinnerungen, Chats   oder remote (HTTP)
```

- **Erinnert sich wie ChatGPT:** In jedem Chat stehen eine Memory-Zusammenfassung über dich (inkl. Antwort-Präferenzen), gespeicherte Erinnerungen und deine letzten Gespräche. Dazu kommen pro Nachricht Steckbriefe erwähnter Personen und Projekte.
- **Lernt im Hintergrund ("Dreaming"):** Nach ein paar Minuten Ruhe wird ein Chat als Ganzes verarbeitet und das Wissen aufgeräumt.
- **Ist strukturiert:** Personen, Projekte (Arbeit/privat), Organisationen, Ideen, Strategien und Ziele mit Beziehungen und Historie.
- **Bindet MCP-Server an wie Claude Code:** Server werden per Befehl gestartet (`npx …`, `uvx --from git+…`), ohne Repo-Klonen und ohne URL-Freigabe.
- **Fragt per Button nach**, bevor etwas verändert wird. Es gibt drei Modi wie in Claude Code: Planen, Fragen, Auto.

Der Aufbau ist in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) beschrieben.

---

## Installation auf dem Mac

Voraussetzungen: [uv](https://docs.astral.sh/uv/) und Node (für die App und Node-basierte MCP-Server).

```bash
brew install uv node
```

```bash
scripts/macos/install.sh
```

Das Skript:

- legt `.env`, `config/mcp.json` und `config/secrets.env` an (falls sie fehlen),
- installiert die Abhängigkeiten und **baut die App**,
- richtet den Harness als **Hintergrunddienst** (LaunchAgent) ein. Er startet beim Login automatisch und nach einem Absturz neu.
- öffnet zum Schluss die App unter http://localhost:8100.

Danach `.env` ausfüllen: mindestens `LITELLM_BASE_URL`, `LITELLM_API_KEY`, `CHAT_MODELS` und `USER_NAME`. Dann neu starten:

```bash
launchctl kickstart -k gui/$(id -u)/com.secondbrain.harness
```

**Als App ins Dock holen:** http://localhost:8100 in Safari öffnen und **Ablage → Zum Dock hinzufügen** wählen (in Chrome: Installieren-Symbol in der Adresszeile). Second Brain läuft dann in einem eigenen Fenster mit Icon. Docker brauchst du dafür nicht.

- **Update:** `git pull`, danach `scripts/macos/install.sh` (immer **ohne** `sudo`)
- **Ohne Hintergrunddienst** (z. B. wenn ein verwalteter Firmen-Mac LaunchAgents blockiert): `scripts/macos/run.sh` startet den Harness im Terminal
- **Logs:** `~/Library/Logs/SecondBrain/harness.log`
- **API-Doku:** http://localhost:8100/docs

`HARNESS_API_KEY` ist optional. Der Harness hört nur auf `localhost`. Setzt du einen Key, fragt die App einmal danach.

---

## Die App

- **Chats** werden in der Seitenleiste nach Datum gruppiert. Du kannst sie durchsuchen, anheften, umbenennen und löschen. Titel entstehen automatisch.
- **Antworten** werden gestreamt, mit Markdown, Tabellen und Code samt Kopieren-Knopf. Reasoning-Modelle zeigen einen einklappbaren **Denkprozess**.
- **Tool-Nutzung** erscheint als kompakte Zeile, etwa "Gedächtnis durchsucht" oder "notes · add_note". Mehrere Aufrufe hintereinander werden zu einer Gruppe ("3 Aktionen") zusammengefasst. Ein Klick zeigt Eingabe und Ergebnis.
- **Freigaben:** Bei Aktionen, die etwas verändern, erscheint eine Karte mit diesen Buttons:
  - **Erlauben**
  - **In diesem Chat immer**
  - **Immer erlauben**
  - **Ablehnen**
- **Modus** (unten im Eingabefeld):
  - **Planen:** nur lesen und planen
  - **Fragen:** Änderungen nach deiner Freigabe (Standard)
  - **Auto:** alles ohne Rückfrage

  Der Modus gilt pro Chat.
- **Modell** und **Denkaufwand** wählst du ebenfalls im Eingabefeld.
- **Inkognito** (oben rechts bei einem neuen Chat): Der Assistent kennt dich, aber nichts aus dem Chat wird gespeichert.
- **Bearbeiten und Neu generieren:** Eine frühere Nachricht kannst du ändern und von dort aus neu fragen.
- **Stopp:** Der bisher erzeugte Teil der Antwort bleibt erhalten.
- **Dokumente & Bilder:** PDF, Word, PowerPoint, Excel, Markdown, Text, CSV, Code und Bilder per Klick, Drag & Drop oder Einfügen. Ein Klick auf die Datei im Chat zeigt, was der Assistent daraus gelesen hat. Details unter [Dokumente](#dokumente-und-bilder).
- **Läuft im Hintergrund weiter:** Fenster zu oder neu geladen? Die Antwort läuft weiter und erscheint wieder, sobald du zurück bist.
- **Gedächtnis-Seite:**
  - Zusammenfassung ansehen und bearbeiten
  - gespeicherte Erinnerungen verwalten
  - Personen, Projekte, Ideen usw. mit allen Fakten und deren Historie durchstöbern
- **Skills:** Anleitungen für wiederkehrende Aufgaben anlegen, importieren (.zip/.skill/SKILL.md) und exportieren. Details unter [Skills](#skills).
- **Tools & MCP:**
  - MCP-Server hinzufügen, bearbeiten, ein- und ausschalten und entfernen, mit Vorlagen für npx, uvx aus Git, lokale Ordner und Remote.
  - Konfiguration aus Claude Code per **„JSON einfügen“** übernehmen.
  - **Secrets** verwalten. Die Werte landen in `config/secrets.env` und werden nie angezeigt.
  - Status, Logs und Neustart der Server.
  - Pro Tool festlegen: Immer erlauben, Immer fragen oder Blockieren.
- **Verbrauch:**
  - Ein Ring im Eingabefeld zeigt, wie voll das Kontextfenster ist. Ein Klick zeigt Tokens und Kosten des Chats.
  - Jede Antwort zeigt Modell, Tokens und Kosten.
  - Kontextfenster und Preise kommen automatisch von LiteLLM (`/model/info`). Für unbekannte Modelle trägst du sie in den Einstellungen ein.
- **Einstellungen:**
  - **Eigene Anweisungen** stehen in jedem Chat.
  - **Nachschlagewerke** sind Verzeichnisse, Glossare oder interne Link-Listen mit Beschreibung. Pro Dokument wählst du:
    - *Immer im Kontext:* Der komplette Inhalt steht in jedem Chat.
    - *Bei Bedarf:* Im Chat stehen nur Titel und Beschreibung. Der Assistent sucht mit `read_reference` gezielt darin.

  Beides liegt nur lokal in `data/brain.db`, nicht im Repo und nicht im Gedächtnis-Export.
  - **Export & Import:** MCP-Server, Nachschlagewerke, Tool-Regeln und eigene Anweisungen als JSON-Paket teilen. Secrets werden nie exportiert, nur ihre Platzhalter.
    - Beim Import gibt es eine Vorschau.
    - Bei Konflikten wählst du: als Kopie hinzufügen, ersetzen oder überspringen.
    - Fehlende Secrets trägst du direkt ein.
    - Pfade, die auf dem neuen Rechner angepasst werden müssen, werden angezeigt.
    - Reine MCP-Configs aus Claude Code lassen sich ebenso importieren.
  - **Modelle & Kosten:** Kontextfenster und Preise je Modell, aus LiteLLM oder manuell.
  - **Verbrauch:** Tokens und Kosten nach Zeitraum, getrennt nach Chats und Hintergrundaufgaben (Lernen, Titel).

Tastenkürzel: `⌘⇧O` öffnet einen neuen Chat, `⌘\` blendet die Seitenleiste ein und aus. Mit `Enter` sendest du, mit `Shift+Enter` beginnt eine neue Zeile.

<details>
<summary>Optional: Open WebUI weiter nutzen</summary>

Der Harness bietet weiterhin eine OpenAI-kompatible API unter `/v1`. Dort erscheinen die Modelle `brain/<modell>` und `incognito/<modell>`. Chats aus Open WebUI tauchen auch in der App auf und lassen sich dort fortsetzen. Freigaben laufen in Open WebUI per Antwort ("ok" / "nein"), der Modus wird per `/plan`, `/ask` oder `/auto` am Anfang einer Nachricht gewechselt.

```bash
docker compose up -d
```

Damit läuft Open WebUI unter http://localhost:3000. Setz in diesem Fall einen `HARNESS_API_KEY`.

</details>

<details>
<summary>Alternativen: ohne LaunchAgent / komplett in Docker</summary>

Manuell starten (vorher die App bauen: `cd web && npm ci && npm run build`):

```bash
uv run uvicorn app.main:app --port 8100
```

Komplett in Docker: MCP-Server laufen dann im Container und kommen nicht an Mac-Apps heran.

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
| `ask` (Standard) | ✅ sofort | ⏸️ Freigabe per Button (in Open WebUI per "ok" / "nein") |
| `auto` | ✅ sofort | ✅ sofort |

- **Modus wechseln:** In der App über den Schalter im Eingabefeld. In Open WebUI schreibst du `/plan`, `/ask` oder `/auto` an den Anfang einer Nachricht. Der Modus gilt jeweils für diesen Chat.
- **Was als lesend gilt:** Ein Tool gilt als lesend, wenn der Server es so markiert (`readOnlyHint`) oder du es in `"readOnly"` einträgst.
- **Regeln aus der Config gehen immer vor:** `deny` blendet ein Tool aus, `ask` fragt immer nach, `allow` erlaubt ohne Rückfrage.
- **Gedächtnis-Tools** sind in allen Modi erlaubt.

---

## Reasoning-Modelle und Responses API

Manche Modelle (z. B. neuere GPT-Modelle über Azure) lehnen Tools zusammen mit `reasoning_effort` auf `/v1/chat/completions` ab. Der Harness wechselt dann **automatisch** auf `/v1/responses`. Dort gehen Tools und Reasoning gleichzeitig, und eine Zusammenfassung des Reasonings erscheint in der App als Denkprozess. Es wird nichts beim Anbieter gespeichert (`store: false`).

Einstellungen dafür: `UPSTREAM_API=responses` (immer), `RESPONSES_MODELS=…` (nur bestimmte Modelle) und `REASONING_TOOLS_CONFLICT` (Verhalten im Konfliktfall).

## Dokumente und Bilder

Hochgeladene Dateien werden beim Upload einmal aufbereitet und unter `data/files/` gespeichert (nur lokal):

| Format | So liest der Assistent es |
|---|---|
| PDF | Text als Markdown mit Überschriften, Listen und Tabellen, Seite für Seite. Seiten mit Grafiken, Diagrammen oder Scans bekommt er **zusätzlich als Bild**. |
| Word (.docx) | Markdown mit Überschriften, Tabellen und Listen |
| PowerPoint (.pptx) | Folie für Folie inkl. Sprechernotizen. Mit LibreOffice zusätzlich die Folien als Bild, damit Diagramme und Layouts ankommen. |
| Excel (.xlsx/.xls) | Jedes Tabellenblatt als Tabelle |
| Bilder | direkt an das Modell (Vision) |
| Text, Markdown, CSV, JSON, Code, HTML | als Text |

**Kleine Dokumente** (bis `DOC_INLINE_TOKENS`, Standard 15 000 Tokens) stehen komplett im Chat. **Große Dokumente** bekommt der Assistent als Gliederung mit Anfang und liest gezielt nach: mit dem Tool `read_document` nach Seiten/Folien/Blättern oder per Suchbegriff. So bleiben auch 300-Seiten-PDFs bezahlbar und das Kontextfenster frei.

Optional:

- **LibreOffice** (`brew install --cask libreoffice`) wird automatisch erkannt. Damit gehen auch alte Formate (.doc, .ppt, .rtf, .odt …) und Folien/Seiten als Bild.
- `PDF_NATIVE=true` schickt PDFs zusätzlich im Original an das Modell, falls dein Modell/Anbieter PDF-Eingaben direkt versteht.
- `DOC_VISION=false` schaltet die Seitenbilder ab (z. B. für Modelle ohne Bildverständnis). `DOC_MAX_PAGE_IMAGES` begrenzt ihre Zahl pro Datei (Standard 6).
- `MAX_UPLOAD_MB` (Standard 50).

Dateien eines Chats werden mit dem Chat gelöscht.

## Skills

Skills folgen dem offenen [Agent-Skills-Format](https://agentskills.io) – dasselbe wie bei Claude Code, Claude.ai oder Codex. Ein Skill ist ein Ordner mit einer `SKILL.md`:

```markdown
---
name: status-mail
description: Schreibt wöchentliche Status-Mails ans Management. Nutzen, wenn ein Wochenupdate gebraucht wird.
---

# Status-Mail
1. Lies references/beispiel.md für Ton und Aufbau.
2. Gliederung: Erreicht, Nächste Schritte, Risiken.
```

Daneben können weitere Dateien liegen (`references/`, `assets/`, `scripts/` …).

- **So nutzt der Assistent sie:** Im Systemprompt stehen nur Name und Beschreibung (wenige Tokens pro Skill). Passt eine Anfrage, lädt er die Anleitung mit `use_skill` und liest Zusatzdateien mit `read_skill_file`. Die Beschreibung entscheidet also, ob ein Skill zum Einsatz kommt.
- **Verwalten** auf der Seite *Skills*: anlegen, bearbeiten, ein-/ausschalten, löschen.
- **Import:** `.zip`/`.skill` mit einem oder mehreren Skill-Ordnern oder eine einzelne `SKILL.md`. Skills aus Claude Code (`~/.claude/skills/<name>`) einfach als Ordner zippen. Bei gleichem Namen: als Kopie, ersetzen oder überspringen.
- **Export:** einzeln oder alle als `.zip` – oder zusammen mit MCP-Servern und Nachschlagewerken im [Paket](#die-app) (Einstellungen → Export).
- **Skripte** (`scripts/…`, `.py`, `.sh`, `.js`) darf der Assistent nur ausführen, wenn du das auf der Skills-Seite einschaltest. Sie laufen mit deinen Rechten in einem eigenen Arbeitsordner pro Chat (`data/workspace/<chat>`), in dem auch die hochgeladenen Dateien des Chats liegen. Im Modus *Fragen* bestätigst du jeden Aufruf, im Modus *Planen* läuft nichts. Python-Skripte nutzen die Python-Umgebung des Harness (u. a. openpyxl, python-pptx, PyMuPDF sind dabei).

Skills liegen in `data/skills/` (`SKILLS_DIR`).

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

Die App mit Hot-Reload entwickeln. Dafür muss der Harness auf `:8100` laufen, die App ist dann unter http://localhost:5173 erreichbar:

```bash
cd web && npm install && npm run dev
```

Die Prompts liegen in `app/prompts/*.md` und lassen sich über `PROMPTS_DIR` durch eigene Versionen ersetzen.

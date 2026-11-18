# Second Brain Harness

Ein Backend für einen persönlichen KI-Assistenten mit **Langzeitgedächtnis** – wie ChatGPT/Claude mit Memory, aber an deine **eigene LiteLLM-API** angebunden und mit allen Daten lokal in einer SQLite-Datei.

Open WebUI ist das Frontend, der Harness sitzt als OpenAI-kompatibler Server dazwischen:

```
Open WebUI ──(OpenAI-API)──▶ Harness ──▶ LiteLLM ──▶ GPT-5 / Claude / …
                               │
                    SQLite: Profil · Entitäten · Fakten · Chats
                               │
                    (optional) Obsidian-Vault als Markdown
```

Was er tut:

- **Erinnert automatisch:** Zu jeder Nachricht werden passende Erinnerungen und Steckbriefe (Personen, Projekte …) in den Kontext geladen, ohne dass du danach fragen musst.
- **Lernt automatisch:** Nach jeder Antwort extrahiert ein Hintergrund-Job strukturiert, was wichtig ist, und gleicht es mit dem bestehenden Wissen ab (neu / aktualisiert / veraltet).
- **Ist strukturiert:** Personen, Projekte (Arbeit/privat), Organisationen, Ideen, Strategien, Ziele und Lebensbereiche mit Beziehungen untereinander, dazu atomare Fakten mit Historie.
- **Kann aktiv nachschlagen:** Das Modell hat Tools (`search_memory`, `get_entity`, `search_past_chats`, `remember`, `forget_fact` …).
- **Hat ein Kernprofil:** Eine kompakte Zusammenfassung über dich, die in jedem Chat steckt und sich laufend selbst aktualisiert.
- **Ist transparent:** Admin-API zum Ansehen und Korrigieren und ein automatischer Export als Obsidian-Vault.

Details zum Aufbau stehen in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Schnellstart (Docker auf dem Mac)

```bash
cp .env.example .env
```

In `.env` mindestens `LITELLM_BASE_URL`, `LITELLM_API_KEY`, `CHAT_MODELS`, `HARNESS_API_KEY` und `USER_NAME` setzen. Danach:

```bash
docker compose up -d --build
```

- Open WebUI: http://localhost:3000 (beim ersten Start einen Account anlegen)
- Harness-Admin-API (Swagger): http://localhost:8100/docs

Im Modell-Dropdown von Open WebUI erscheinen:

| Modell | Verhalten |
|---|---|
| `brain/gpt-5` | Normaler Chat mit Gedächtnis (liest und lernt) |
| `incognito/gpt-5` | Liest das Gedächtnis, speichert aber **nichts** |

### Open WebUI schon vorhanden?

Den `open-webui`-Service in `docker-compose.yml` entfernen und in Open WebUI unter **Admin-Einstellungen → Verbindungen → OpenAI** eine Verbindung anlegen:

- URL: `http://host.docker.internal:8100/v1`
- Key: dein `HARNESS_API_KEY`

Zusätzlich die Umgebungsvariable `ENABLE_FORWARD_USER_INFO_HEADERS=true` für Open WebUI setzen. Ohne sie funktioniert es auch, die Zuordnung von Chats ist dann aber ungenauer.

### Empfohlene Einstellungen in Open WebUI

- Die **eingebaute Memory-Funktion** (Einstellungen → Personalisierung) ausgeschaltet lassen, sonst gibt es doppeltes Gedächtnis.
- Unter **Admin → Einstellungen → Oberfläche** kann für Titel- und Tag-Generierung ein eigenes "Task-Modell" gewählt werden. Der Harness erkennt diese Anfragen aber auch selbst und reicht sie ohne Gedächtnis durch.
- **Function Calling** im Modell kann auf "Default" bleiben. Die Gedächtnis-Tools laufen im Harness, nicht in Open WebUI.
- **Reasoning-Modelle:** Siehe den Abschnitt "Reasoning-Modelle und Responses API" weiter unten.

---

## Lokal ohne Docker

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -r requirements-dev.txt
```

```bash
cp .env.example .env
```

In `.env` `LITELLM_BASE_URL=http://localhost:4000` setzen und danach:

```bash
.venv/bin/uvicorn app.main:app --port 8100
```

Tests:

```bash
.venv/bin/python -m pytest
```

---

## Reasoning-Modelle und Responses API

Manche Modelle (z. B. neuere GPT-Modelle über Azure) erlauben auf `/v1/chat/completions` keine Tools zusammen mit `reasoning_effort`. Über `/v1/responses` geht beides. Der Harness kann deshalb nach hinten die **Responses API** von LiteLLM nutzen. Open WebUI merkt davon nichts, nach vorne bleibt alles Chat Completions.

Es gibt drei Wege, die Responses API zu nutzen:

- **Automatisch (Standard):** Lehnt ein Modell Tools mit Reasoning ab, wechselt der Harness für dieses Modell selbstständig auf `/v1/responses`. Funktioniert auch das nicht, antwortet er über Chat Completions ohne Gedächtnis-Tools.
- **Immer:** `UPSTREAM_API=responses`
- **Nur für bestimmte Modelle:** `RESPONSES_MODELS=gpt-6.1,…`

Über die Responses API läuft das so:

- **Tools und Reasoning gleichzeitig:** Das Modell kann während des Nachdenkens im Gedächtnis nachschlagen.
- **Denkprozess sichtbar:** Reasoning-Zusammenfassungen erscheinen in Open WebUI als einklappbarer Denkprozess. Abschalten mit `RESPONSES_REASONING_SUMMARY=`.
- **Nichts wird beim Anbieter gespeichert** (`store: false`). Das verschlüsselte Reasoning wird nur innerhalb einer Antwort zwischen den Tool-Schritten zurückgereicht.
- **Optionale Features fallen automatisch weg:** Lehnt der Anbieter die Reasoning-Zusammenfassung oder das verschlüsselte Reasoning ab, schaltet der Harness es selbst ab und wiederholt die Anfrage.

Wenn die Responses API nicht verfügbar ist, regelt `REASONING_TOOLS_CONFLICT`, was im Konfliktfall passiert: `use_responses` (Standard), `prefer_reasoning` (ohne Tools antworten) oder `prefer_tools` (`reasoning_effort=none`).

## Embeddings: brauche ich die?

Nicht zwingend. Ohne Embeddings findet der Harness Erinnerungen über **Volltextsuche** (inklusive Teilwörtern: "Wohnung" findet auch "Mietwohnung") und über die **Namenserkennung** von Personen, Projekten usw.

Mit Embeddings kommt **semantische Suche** dazu: "Ich bin total erschöpft" findet dann auch "Hatte im Oktober eine stressige Phase mit wenig Schlaf". Dafür in LiteLLM ein Embedding-Modell eintragen (z. B. `text-embedding-3-small`, oder lokal `bge-m3` über Ollama) und in `.env` `EMBEDDING_MODEL=<name>` setzen. Bestehende Einträge werden beim nächsten Start automatisch eingebettet. Nach einem **Wechsel** des Modells:

```bash
curl -X POST -H "Authorization: Bearer $HARNESS_API_KEY" http://localhost:8100/memory/reembed
```

---

## Gedächtnis ansehen und pflegen

Alles ist unter http://localhost:8100/docs dokumentiert. Die wichtigsten Endpunkte:

| Endpunkt | Zweck |
|---|---|
| `GET /memory/profile` · `POST /memory/profile/rebuild` | Kernprofil ansehen / neu erstellen |
| `GET /memory/entities?type=person` | Alle Personen (oder `project`, `idea`, `strategy`, …) |
| `GET /memory/entities/{id}` | Steckbrief mit allen Fakten (inkl. Historie) und Beziehungen |
| `PATCH /memory/entities/{id}` | Korrigieren, z. B. `{"status": "archived"}` |
| `POST /memory/entities/{keep}/merge/{dup}` | Duplikate zusammenführen |
| `GET /memory/facts?q=…` · `PATCH/DELETE /memory/facts/{id}` | Fakten suchen, korrigieren, löschen |
| `GET /memory/search?q=…` | Was würde der Assistent zu dieser Nachricht erinnern? |
| `GET /memory/conversations` | Gespeicherte Chats mit Zusammenfassung |
| `GET /memory/stats` · `GET /memory/jobs` | Statistik und Hintergrund-Jobs |

Im Chat geht es auch direkt: "Merk dir, dass …", "Vergiss das mit …", "Was weißt du über Max?", "Was hatten wir letzte Woche zu Projekt X besprochen?".

### Obsidian-Vault

Wenn `EXPORT_DIR` gesetzt ist (in Docker: `./vault`), wird das Gedächtnis nach Änderungen automatisch als Markdown exportiert: eine Notiz pro Person, Projekt, Idee usw., mit `[[Wikilinks]]`, dazu `Profil.md` und die Chat-Zusammenfassungen. Den Ordner einfach als Vault in Obsidian öffnen, dann siehst du dein Second Brain auch als Graph.

> Der Export geht nur in eine Richtung. Änderungen an generierten Notizen werden beim nächsten Export überschrieben. Eigene Notizen ohne `generated_by: second-brain-harness` im Frontmatter bleiben unangetastet.

---

## Backup

Dein gesamtes Gedächtnis steckt in `data/brain.db`. Ein Backup im laufenden Betrieb:

```bash
sqlite3 data/brain.db ".backup data/brain-$(date +%F).db"
```

## Prompts anpassen

Die Prompts liegen in `app/prompts/*.md`: `system.md` (Persona), `extraction.md` (was gelernt wird), `profile.md` und `summary.md`. Eigene Versionen können über `PROMPTS_DIR=/pfad/zu/prompts` eingebunden werden. Fehlende Dateien fallen auf die Standardversion zurück.

# Architektur

## Überblick

```
Open WebUI ──(OpenAI-API)──▶ Harness (FastAPI) ──▶ LiteLLM
                               │
                     ┌─────────┴───────────────┐
                     │ 1. Kontext laden          │  vor der Antwort, synchron
                     │ 2. Agent-Loop mit Tools   │  während der Antwort
                     │ 3. Lernen                 │  nach der Antwort, Hintergrund-Job
                     └─────────┬───────────────┘
                          SQLite (WAL) + FTS5
```

Der Harness ist ein OpenAI-kompatibler Server (`/v1/models`, `/v1/chat/completions` mit SSE-Streaming). Open WebUI braucht keine Anpassung.

## Gedächtnis in drei Schichten

| Schicht | Inhalt | Wann im Kontext |
|---|---|---|
| **Kernprofil** | Kompakte Markdown-Zusammenfassung über den Nutzer (~450 Wörter) | Immer |
| **Strukturiertes Wissen** | Entitäten, Fakten, Relationen | Automatisch nach Relevanz plus per Tool |
| **Episodisch** | Alle Chat-Turns im Wortlaut plus Zusammenfassung pro Chat | Per Tool `search_past_chats` |

## Datenmodell (`app/db.py`)

- **entities**: `type` ∈ self, person, project, organization, idea, strategy, goal, life_area, topic. Dazu `name`, `aliases` (JSON), `summary`, `attributes` (JSON, z. B. `bereich`, `status`, `firma`), `status` (active/archived) und `embedding`.
- **facts**: atomare Aussagen mit `category`, `importance` (1–5), `status` (active/superseded/deleted), `superseded_by`, Quelle (Chat und Turn) und `embedding`. Fakten werden nie überschrieben, sondern **abgelöst**, damit die Historie erhalten bleibt.
- **fact_entities**: n:m-Verknüpfung zwischen Fakt und Entität.
- **relations**: gerichtete Kanten zwischen Entitäten (`arbeitet_bei`, `leitet`, `kennt` …).
- **conversations / turns**: Chats mit Titel, Zusammenfassung und Embedding; Turns mit Nutzer- und Assistenten-Nachricht und Tool-Log.
- **profile**: genau eine Zeile mit dem Kernprofil und einem Änderungszähler.
- **jobs**: persistente Job-Queue (übersteht Neustarts, Retry mit Backoff, Deduplizierung über `dedupe_key`).
- **facts_fts / turns_fts**: FTS5-Volltextindex mit Trigram-Tokenizer, der Teilwörter findet (wichtig für deutsche Komposita).

## Lesepfad (`app/memory/retrieval.py`)

1. **Entity-Detection**: Namen und Aliase aller aktiven Entitäten werden in der aktuellen Nachricht gesucht, ergänzend in der vorherigen Nachricht und der letzten Antwort.
2. **Hybrid-Suche**:
   - FTS5/BM25 über die Fakten
   - optional Kosinus-Ähnlichkeit über Embeddings (numpy, Brute-Force)
   - die wichtigsten Fakten der erkannten Entitäten
3. **Reciprocal Rank Fusion** der Ranglisten, gewichtet mit `importance` und Aktualität (exponentieller Zerfall, ~4 Monate).
4. **Rendering** im Token-Budget (`CONTEXT_TOKEN_BUDGET`): Steckbriefe der erkannten Entitäten (inkl. Beziehungen) plus relevante Fakten mit ID und Datum.

Bei kurzen Rückfragen ("und was meinst du dazu?") wird die vorherige Nutzernachricht in die Suchanfrage einbezogen.

## Agent-Loop (`app/agent/session.py`)

- Der Systemprompt besteht aus Persona, Datum und Uhrzeit, Kernprofil und abgerufenen Erinnerungen. Ein eventueller Systemprompt von Open WebUI bleibt danach erhalten.
- Die Gedächtnis-Tools werden serverseitig ausgeführt, bis zu `MAX_TOOL_ITERATIONS` Runden. In der letzten Runde werden keine Tools mehr angeboten, damit das Modell antworten muss.
- Text, Reasoning und Usage werden live an Open WebUI gestreamt. Tool-Aktivität erscheint optional als einklappbarer `<details type="memory">`-Block. Dieser wird aus der Historie wieder entfernt und nicht gespeichert.
- Tools, die Open WebUI selbst mitschickt (native Function Calling), werden an den Client durchgereicht.
- **Inkognito-Modus**: liest das Gedächtnis, bekommt keine Schreib-Tools, speichert nichts.
- **Open-WebUI-Hintergrundaufgaben** (Titel, Tags, Follow-ups; erkennbar an `### Task:`) werden ohne Gedächtnis durchgereicht.
- **Chat-Zuordnung** läuft über den Header `X-OpenWebUI-Chat-Id`, als Fallback über einen Hash der ersten Nachricht. Der Turn-Index ist die Anzahl der Nutzernachrichten. Eine neu generierte Antwort ersetzt deshalb den bestehenden Turn, statt einen neuen anzulegen.

## Schreibpfad (`app/memory/extraction.py`, `app/jobs.py`)

Nach jeder Antwort wird ein Job `extract_turn` angelegt:

1. **Kandidaten** sammeln: erkannte und semantisch ähnliche Entitäten, bestehende Fakten zum Thema (mit IDs), die letzten zwei Turns als Kontext und bereits per Tool gespeicherte Inhalte.
2. **Ein LLM-Call** mit JSON-Output erledigt Extraktion und Konsolidierung zugleich: Entitäten (bestehende als `E<id>`, neue als `N1`…), Fakten mit `add` / `supersede` / `delete` und Relationen.
3. **Validierung und Anwendung** in einer Transaktion. Dabei greifen ein Duplikat-Schutz für Entitäten (gleicher Name oder Alias und Typ) und für Fakten (normalisiert identischer Inhalt). Ungültige Referenzen werden ignoriert.
4. **Folgejobs**:
   - Embeddings nachziehen
   - nach `PROFILE_REBUILD_EVERY` Änderungen das Profil neu bauen
   - Vault-Export (entprellt)

Nach `SUMMARY_IDLE_MINUTES` Inaktivität wird der Chat zusammengefasst und mit Titel und Embedding für `search_past_chats` gespeichert.

## Warum SQLite?

Ein Nutzer, einige tausend bis zehntausend Fakten: Für diesen Umfang sind SQLite mit WAL, FTS5 und Brute-Force-Vektorsuche schnell genug (Millisekunden). Das Backup ist eine einzige Datei. Der Harness läuft deshalb bewusst als **ein** Prozess. Worker und API teilen sich die Verbindung.

## Mögliche nächste Schritte

- Wöchentlicher Review-Job: offene Follow-ups bei Personen, ruhende Ideen, Projekt-Deadlines
- Zwei-Wege-Sync mit dem Obsidian-Vault
- Kleine Web-Oberfläche zum Durchstöbern und Korrigieren des Gedächtnisses
- Projekt-Modi (`work/…`), die beim Retrieval Arbeitsprojekte priorisieren
- Konsolidierungs-Job, der semantisch ähnliche Fakten zusammenführt

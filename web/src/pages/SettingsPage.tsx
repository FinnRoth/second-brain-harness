import { BookText, Check, Download, FileUp, Lock, Pencil, Plus, RefreshCw, Trash2, Upload } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { AuthError, request } from "../api";
import { ExportDialog, ImportDialog } from "../components/BundleDialogs";
import { fmtCost, fmtTokens } from "../format";

interface RefDoc {
  id: number;
  title: string;
  description: string;
  content?: string;
  mode: "always" | "on_demand";
  enabled: boolean;
  chars: number;
  updated_at: string;
}

const tokens = (chars: number) => Math.max(0, Math.round(chars / 4));
const fmt = (n: number) => n.toLocaleString("de-DE");

function Instructions({ onAuthError }: { onAuthError: () => void }) {
  const [text, setText] = useState<string | null>(null);
  const [saved, setSaved] = useState("");
  const [state, setState] = useState<"idle" | "saving" | "saved">("idle");

  useEffect(() => {
    request<{ custom_instructions: string }>("GET", "/api/settings")
      .then((s) => {
        setText(s.custom_instructions);
        setSaved(s.custom_instructions);
      })
      .catch((e) => e instanceof AuthError && onAuthError());
  }, [onAuthError]);

  const save = async () => {
    if (text === null) return;
    setState("saving");
    const r = await request<{ custom_instructions: string }>("PUT", "/api/settings", { custom_instructions: text });
    setSaved(r.custom_instructions);
    setState("saved");
    setTimeout(() => setState("idle"), 1500);
  };

  return (
    <div className="card">
      <div className="card-head">
        <h3>Eigene Anweisungen</h3>
        <span className="muted small">stehen in jedem Chat</span>
      </div>
      <p className="muted small">
        Wie soll sich der Assistent verhalten? Z. B. Rolle, Tonfall, Fachbegriffe, Regeln für den Umgang mit internen
        Systemen.
      </p>
      <textarea
        className="input tall-sm"
        placeholder="z. B. Ich arbeite bei … als …. Antworte knapp und strukturiert. Verlinke interne Systeme immer mit der vollständigen URL."
        value={text ?? ""}
        disabled={text === null}
        onChange={(e) => setText(e.target.value)}
      />
      <div className="edit-actions">
        <span className="muted small grow">~{fmt(tokens((text ?? "").length))} Tokens</span>
        <button className="btn primary" disabled={text === null || text === saved || state === "saving"} onClick={save}>
          {state === "saved" ? (
            <>
              <Check size={15} /> Gespeichert
            </>
          ) : (
            "Speichern"
          )}
        </button>
      </div>
    </div>
  );
}

function RefEditor({
  doc,
  onClose,
  onSaved,
}: {
  doc: RefDoc | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [title, setTitle] = useState(doc?.title ?? "");
  const [description, setDescription] = useState(doc?.description ?? "");
  const [mode, setMode] = useState<RefDoc["mode"]>(doc?.mode ?? "on_demand");
  const [content, setContent] = useState<string | null>(doc ? null : "");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const file = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (doc) request<RefDoc>("GET", `/api/references/${doc.id}`).then((d) => setContent(d.content ?? ""));
  }, [doc]);

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const body = { title, description, mode, content: content ?? "" };
      if (doc) await request("PATCH", `/api/references/${doc.id}`, body);
      else await request("POST", "/api/references", body);
      onSaved();
    } catch (e: any) {
      setError(e.message);
      setBusy(false);
    }
  };

  const chars = (content ?? "").length;
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal wide ref-editor" onClick={(e) => e.stopPropagation()}>
        <h2>{doc ? "Nachschlagewerk bearbeiten" : "Neues Nachschlagewerk"}</h2>
        <label className="field">
          <span>Titel</span>
          <input className="input" value={title} autoFocus onChange={(e) => setTitle(e.target.value)} placeholder="z. B. Interne Links" />
        </label>
        <label className="field">
          <span>Wann ist das nützlich?</span>
          <input
            className="input"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="z. B. Verzeichnis interner SAP-URLs (Jira, Wiki, HR-Portale) mit Beschreibung"
          />
        </label>
        <div className="field">
          <span>Einbindung</span>
          <div className="segmented">
            <button className={mode === "on_demand" ? "active" : ""} onClick={() => setMode("on_demand")}>
              Bei Bedarf nachschlagen
            </button>
            <button className={mode === "always" ? "active" : ""} onClick={() => setMode("always")}>
              Immer im Kontext
            </button>
          </div>
          <span className="muted small">
            {mode === "on_demand"
              ? "Im Prompt stehen nur Titel und Beschreibung; der Assistent sucht bei Bedarf gezielt darin. Gut für große Verzeichnisse."
              : "Der komplette Inhalt steht in jedem Chat. Gut für Kurzes, das fast immer relevant ist."}
          </span>
        </div>
        <div className="field grow-field">
          <span className="field-head">
            Inhalt
            <span className="grow" />
            <span className={`muted small${mode === "always" && chars > 32000 ? " warn" : ""}`}>
              {fmt(chars)} Zeichen · ~{fmt(tokens(chars))} Tokens
            </span>
            <button className="btn small" onClick={() => file.current?.click()}>
              <FileUp size={14} /> Aus Datei
            </button>
            <input
              ref={file}
              type="file"
              hidden
              accept=".md,.txt,.csv,.json,.yaml,.yml,.html,.xml,text/*"
              onChange={async (e) => {
                const f = e.target.files?.[0];
                if (f) {
                  setContent(await f.text());
                  if (!title) setTitle(f.name.replace(/\.[^.]+$/, ""));
                }
                e.target.value = "";
              }}
            />
          </span>
          <textarea
            className="input mono ref-content"
            value={content ?? ""}
            disabled={content === null}
            onChange={(e) => setContent(e.target.value)}
            placeholder={"# Interne Links\n\n## Projektmanagement\n- Jira Team X: https://… – Tickets & Sprints\n- Wiki: https://… – Doku"}
          />
        </div>
        {error && <div className="form-error">{error}</div>}
        <div className="edit-actions">
          <button className="btn" onClick={onClose}>
            Abbrechen
          </button>
          <button className="btn primary" disabled={busy || !title.trim() || content === null} onClick={save}>
            Speichern
          </button>
        </div>
      </div>
    </div>
  );
}

function References({ onAuthError }: { onAuthError: () => void }) {
  const [docs, setDocs] = useState<RefDoc[] | null>(null);
  const [editing, setEditing] = useState<RefDoc | null | "new">(null);

  const load = useCallback(() => {
    request<RefDoc[]>("GET", "/api/references")
      .then(setDocs)
      .catch((e) => e instanceof AuthError && onAuthError());
  }, [onAuthError]);
  useEffect(load, [load]);

  const alwaysTokens = (docs ?? []).filter((d) => d.enabled && d.mode === "always").reduce((a, d) => a + tokens(d.chars), 0);

  return (
    <div className="card list">
      <div className="list-title row">
        <span>Nachschlagewerke</span>
        <span className="grow" />
        {alwaysTokens > 0 && <span className="muted small">immer im Kontext: ~{fmt(alwaysTokens)} Tokens</span>}
        <button className="btn small primary" onClick={() => setEditing("new")}>
          <Plus size={14} /> Hinzufügen
        </button>
      </div>
      {docs?.length === 0 && (
        <div className="pad muted">
          Hier kannst du Verzeichnisse, Glossare oder Übersichten hinterlegen – z. B. interne Links mit Beschreibungen.
          Der Assistent nutzt sie, wenn sie zur Frage passen.
        </div>
      )}
      {docs?.map((d) => (
        <div key={d.id} className={`tool-row${d.enabled ? "" : " disabled-row"}`}>
          <BookText size={18} className="muted" />
          <div className="tool-row-text">
            <div>
              <b>{d.title}</b>{" "}
              <span className={`kind ${d.mode === "always" ? "kind-memory" : "kind-read"}`}>
                {d.mode === "always" ? "immer im Kontext" : "bei Bedarf"}
              </span>
            </div>
            <div className="muted small ellipsis">
              {d.description || "—"} · {fmt(d.chars)} Zeichen
            </div>
          </div>
          <label className="switch" title={d.enabled ? "Aktiv" : "Deaktiviert"}>
            <input
              type="checkbox"
              checked={d.enabled}
              onChange={async (e) => {
                await request("PATCH", `/api/references/${d.id}`, { enabled: e.target.checked });
                load();
              }}
            />
            <span />
          </label>
          <button className="icon-btn small" title="Bearbeiten" onClick={() => setEditing(d)}>
            <Pencil size={14} />
          </button>
          <button
            className="icon-btn small"
            title="Löschen"
            onClick={async () => {
              if (!confirm(`„${d.title}“ löschen?`)) return;
              await request("DELETE", `/api/references/${d.id}`);
              load();
            }}
          >
            <Trash2 size={14} />
          </button>
        </div>
      ))}
      {editing !== null && (
        <RefEditor
          doc={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            load();
          }}
        />
      )}
    </div>
  );
}

interface ModelRow {
  model: string;
  role: string;
  context_window: number | null;
  input_cost_per_mtok: number | null;
  output_cost_per_mtok: number | null;
  cached_cost_per_mtok: number | null;
  source: Record<string, string | null>;
  litellm: Record<string, number | null>;
}

const MODEL_FIELDS: { key: keyof ModelRow; label: string; unit: string }[] = [
  { key: "context_window", label: "Kontextfenster", unit: "Tokens" },
  { key: "input_cost_per_mtok", label: "Eingabe", unit: "$ / 1 Mio." },
  { key: "cached_cost_per_mtok", label: "Eingabe (Cache)", unit: "$ / 1 Mio." },
  { key: "output_cost_per_mtok", label: "Ausgabe", unit: "$ / 1 Mio." },
];

function ModelEditorRow({ row, onSaved }: { row: ModelRow; onSaved: () => void }) {
  const initial = () =>
    Object.fromEntries(MODEL_FIELDS.map((f) => [f.key, row.source[f.key] === "manuell" ? String(row[f.key] ?? "") : ""]));
  const [vals, setVals] = useState<Record<string, string>>(initial);
  const [state, setState] = useState<"idle" | "saved">("idle");
  const dirty = MODEL_FIELDS.some((f) => vals[f.key] !== initial()[f.key]);

  const save = async () => {
    const body: Record<string, number | null> = {};
    for (const f of MODEL_FIELDS) body[f.key] = vals[f.key].trim() === "" ? null : Number(vals[f.key].replace(",", "."));
    await request("PUT", `/api/models/${encodeURIComponent(row.model)}`, body);
    setState("saved");
    setTimeout(() => setState("idle"), 1500);
    onSaved();
  };

  return (
    <tr>
      <td>
        <b>{row.model}</b>
        <span className="src-badge">{row.role}</span>
      </td>
      {MODEL_FIELDS.map((f) => {
        const lite = row.litellm?.[f.key as string];
        return (
          <td key={f.key}>
            <input
              className="input"
              inputMode="decimal"
              value={vals[f.key]}
              placeholder={lite != null ? String(Math.round(Number(lite) * 1000) / 1000) : "unbekannt"}
              onChange={(e) => setVals({ ...vals, [f.key]: e.target.value })}
            />
            <span className="src-badge">
              {row.source[f.key] === "manuell" ? "manuell" : lite != null ? "aus LiteLLM" : "–"}
            </span>
          </td>
        );
      })}
      <td>
        <button className="btn small" disabled={!dirty} onClick={save}>
          {state === "saved" ? <Check size={14} /> : "Speichern"}
        </button>
      </td>
    </tr>
  );
}

function ModelsCard({ onAuthError }: { onAuthError: () => void }) {
  const [rows, setRows] = useState<ModelRow[] | null>(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback(
    (refresh = false) => {
      setBusy(true);
      request<ModelRow[]>("GET", `/api/models${refresh ? "?refresh=true" : ""}`)
        .then(setRows)
        .catch((e) => e instanceof AuthError && onAuthError())
        .finally(() => setBusy(false));
    },
    [onAuthError],
  );
  useEffect(() => load(), [load]);
  return (
    <div className="card list">
      <div className="list-title row">
        <span>Modelle & Kosten</span>
        <span className="grow" />
        <button className="btn small" disabled={busy} onClick={() => load(true)} title="Werte erneut von LiteLLM holen">
          <RefreshCw size={13} className={busy ? "spin" : ""} /> Von LiteLLM laden
        </button>
      </div>
      <p className="pad muted small" style={{ paddingBottom: 0 }}>
        Kontextfenster und Preise kommen automatisch von LiteLLM. Kennt LiteLLM ein Modell nicht (z. B. eigene Deployments),
        trag die Werte hier ein – leere Felder nutzen wieder den LiteLLM-Wert.
      </p>
      <div className="table-scroll">
        <table className="model-table">
          <thead>
            <tr>
              <th>Modell</th>
              {MODEL_FIELDS.map((f) => (
                <th key={f.key}>
                  {f.label}
                  <span className="src-badge">{f.unit}</span>
                </th>
              ))}
              <th />
            </tr>
          </thead>
          <tbody>{rows?.map((r) => <ModelEditorRow key={r.model} row={r} onSaved={() => load()} />)}</tbody>
        </table>
      </div>
    </div>
  );
}

interface UsageTotals {
  prompt_tokens: number;
  completion_tokens: number;
  cached_tokens: number;
  cost: number | null;
  calls: number;
  unpriced: number;
}

function UsageCard({ onAuthError }: { onAuthError: () => void }) {
  const [days, setDays] = useState(30);
  const [data, setData] = useState<{
    total: UsageTotals;
    by_kind: (UsageTotals & { kind: string })[];
    by_model: (UsageTotals & { model: string })[];
  } | null>(null);
  useEffect(() => {
    request("GET", `/api/usage?days=${days}`)
      .then(setData)
      .catch((e) => e instanceof AuthError && onAuthError());
  }, [days, onAuthError]);
  const kind = (k: string) => data?.by_kind.find((x) => x.kind === k);
  const tokens = (t?: UsageTotals) => (t ? t.prompt_tokens + t.completion_tokens : 0);
  return (
    <div className="card">
      <div className="card-head">
        <h3>Verbrauch</h3>
        <span className="grow" />
        <select className="select" value={days} onChange={(e) => setDays(Number(e.target.value))}>
          <option value={1}>Heute</option>
          <option value={7}>7 Tage</option>
          <option value={30}>30 Tage</option>
          <option value={365}>12 Monate</option>
        </select>
      </div>
      {data && (
        <>
          <div className="usage-cards">
            <div className="stat">
              <b>{fmtCost(data.total.cost)}</b>Kosten gesamt{data.total.unpriced ? " (ohne Preise: teilweise)" : ""}
            </div>
            <div className="stat">
              <b>{fmtTokens(tokens(data.total))}</b>Tokens gesamt
            </div>
            <div className="stat">
              <b>{fmtTokens(tokens(kind("chat")))}</b>in Chats · {fmtCost(kind("chat")?.cost)}
            </div>
            <div className="stat">
              <b>{fmtTokens(tokens(kind("hintergrund")))}</b>Hintergrund (Lernen, Titel) · {fmtCost(kind("hintergrund")?.cost)}
            </div>
          </div>
          {data.by_model.length > 0 && (
            <div className="table-scroll" style={{ marginTop: 12 }}>
              <table className="model-table">
                <thead>
                  <tr>
                    <th>Modell</th>
                    <th>Eingabe</th>
                    <th>davon Cache</th>
                    <th>Ausgabe</th>
                    <th>Kosten</th>
                  </tr>
                </thead>
                <tbody>
                  {data.by_model.map((m) => (
                    <tr key={m.model ?? "?"}>
                      <td>{m.model ?? "–"}</td>
                      <td>{m.prompt_tokens.toLocaleString("de-DE")}</td>
                      <td>{m.cached_tokens.toLocaleString("de-DE")}</td>
                      <td>{m.completion_tokens.toLocaleString("de-DE")}</td>
                      <td>{fmtCost(m.cost)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function BundlesCard({ onImported }: { onImported: () => void }) {
  const [dialog, setDialog] = useState<"export" | "import" | null>(null);
  return (
    <div className="card">
      <div className="card-head">
        <h3>Export & Import</h3>
        <span className="grow" />
        <button className="btn small" onClick={() => setDialog("import")}>
          <Upload size={14} /> Importieren
        </button>
        <button className="btn small primary" onClick={() => setDialog("export")}>
          <Download size={14} /> Exportieren
        </button>
      </div>
      <p className="muted small">
        Bündle MCP-Server, Nachschlagewerke und Regeln zu einer Datei – z. B. um ein Setup mit Kolleg:innen zu teilen oder
        auf einen anderen Rechner zu übertragen. Secrets bleiben immer bei dir.
      </p>
      {dialog === "export" && <ExportDialog onClose={() => setDialog(null)} />}
      {dialog === "import" && <ImportDialog onClose={() => setDialog(null)} onDone={onImported} />}
    </div>
  );
}

export function SettingsPage({ onAuthError }: { onAuthError: () => void }) {
  const [rev, setRev] = useState(0);
  return (
    <div className="page">
      <header className="page-header">
        <h1>Einstellungen</h1>
      </header>
      <div className="page-body">
        <div className="mem-section">
          <div className="privacy-note">
            <Lock size={15} />
            <span>
              Alles hier wird nur lokal in deiner Datenbank gespeichert – nicht im Repository, nicht im Gedächtnis-Export.
              Beim Chatten wird es an dein LLM geschickt, wie der Rest des Kontexts.
            </span>
          </div>
          <Instructions key={`i${rev}`} onAuthError={onAuthError} />
          <References key={`r${rev}`} onAuthError={onAuthError} />
          <BundlesCard onImported={() => setRev((r) => r + 1)} />
          <ModelsCard onAuthError={onAuthError} />
          <UsageCard onAuthError={onAuthError} />
        </div>
      </div>
    </div>
  );
}

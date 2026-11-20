import { BookText, Check, FileUp, Lock, Pencil, Plus, Trash2 } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { AuthError, request } from "../api";

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

export function SettingsPage({ onAuthError }: { onAuthError: () => void }) {
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
          <Instructions onAuthError={onAuthError} />
          <References onAuthError={onAuthError} />
        </div>
      </div>
    </div>
  );
}

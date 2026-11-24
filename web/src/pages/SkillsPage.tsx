import { AlertTriangle, Check, Download, FileCode2, FileText, Pencil, Plus, Sparkles, Trash2, Upload } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { AuthError, downloadFile, postForm, request } from "../api";
import { Markdown } from "../components/Markdown";

interface SkillSummary {
  name: string;
  description: string;
  enabled: boolean;
  files: number;
  scripts: number;
  chars: number;
  error: string | null;
}

interface SkillFile {
  path: string;
  size: number;
  text: boolean;
}

interface SkillDetail extends SkillSummary {
  body: string;
  meta: Record<string, unknown>;
  file_list: SkillFile[];
  dir: string;
}

const fmtSize = (n: number) => (n < 1024 ? `${n} B` : n < 1024 * 1024 ? `${Math.round(n / 1024)} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`);
const toName = (s: string) =>
  s
    .toLowerCase()
    .replace(/[äöüß]/g, (c) => ({ ä: "ae", ö: "oe", ü: "ue", ß: "ss" })[c] as string)
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+/, "")
    .slice(0, 64);

const TEMPLATE = `# Titel des Skills

## Wann
Kurz, in welchen Situationen dieser Skill gilt.

## Vorgehen
1. …
2. …

## Format
Wie das Ergebnis aussehen soll (Gliederung, Tonfall, Länge).
`;

function FilePreview({ skill, file, onClose }: { skill: string; file: SkillFile; onClose: () => void }) {
  const [text, setText] = useState<string | null | undefined>(undefined);
  useEffect(() => {
    request<{ text: string | null }>("GET", `/api/skills/${skill}/files/${file.path}`).then((r) => setText(r.text));
  }, [skill, file.path]);
  return (
    <div className="skill-file-preview">
      <div className="row">
        <b className="mono small">{file.path}</b>
        <span className="grow" />
        <button className="btn small" onClick={onClose}>
          Schließen
        </button>
      </div>
      {text === undefined ? (
        <div className="muted small">lädt …</div>
      ) : text === null ? (
        <div className="muted small">Binärdatei ({fmtSize(file.size)}) – keine Vorschau.</div>
      ) : file.path.endsWith(".md") ? (
        <div className="doc-body">
          <Markdown text={text} />
        </div>
      ) : (
        <pre className="doc-raw doc-body">{text}</pre>
      )}
    </div>
  );
}

function SkillEditor({ name, onClose, onSaved }: { name: string | null; onClose: () => void; onSaved: () => void }) {
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [skillName, setSkillName] = useState("");
  const [description, setDescription] = useState("");
  const [body, setBody] = useState<string | null>(name ? null : TEMPLATE);
  const [preview, setPreview] = useState<SkillFile | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!name) return;
    request<SkillDetail>("GET", `/api/skills/${name}`).then((d) => {
      setDetail(d);
      setSkillName(d.name);
      setDescription(d.description);
      setBody(d.body);
    });
  }, [name]);

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const payload = { name: skillName, description, body: body ?? "" };
      if (name) await request("PUT", `/api/skills/${name}`, payload);
      else await request("POST", "/api/skills", payload);
      onSaved();
    } catch (e: any) {
      setError(e.message);
      setBusy(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal wide ref-editor skill-editor" onClick={(e) => e.stopPropagation()}>
        <h2>{name ? "Skill bearbeiten" : "Neuer Skill"}</h2>
        <div className="form-grid">
          <label className="field">
            <span>Name</span>
            <input
              className="input mono"
              value={skillName}
              autoFocus={!name}
              onChange={(e) => setSkillName(toName(e.target.value))}
              placeholder="z. B. quartalsbericht"
            />
          </label>
          <label className="field">
            <span>
              Wann soll der Assistent ihn nutzen? <span className="muted">– das Wichtigste</span>
            </span>
            <input
              className="input"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="z. B. Erstellt Quartalsberichte im Firmenformat. Nutzen, wenn ein Q-Report gebraucht wird."
            />
          </label>
        </div>
        <div className="field grow-field">
          <span className="field-head">
            Anleitung (Markdown)
            <span className="grow" />
            <span className="muted small">wird erst geladen, wenn der Skill passt</span>
          </span>
          <textarea
            className="input mono ref-content"
            value={body ?? ""}
            disabled={body === null}
            onChange={(e) => setBody(e.target.value)}
          />
        </div>
        {detail && detail.file_list.length > 0 && (
          <div className="field">
            <span>Dateien im Skill</span>
            <div className="skill-files">
              {detail.file_list.map((f) => (
                <button key={f.path} className="file-chip clickable" onClick={() => setPreview(f)}>
                  {f.path.startsWith("scripts/") ? <FileCode2 size={13} /> : <FileText size={13} />}
                  <span className="chip-name mono">{f.path}</span>
                  <span className="chip-info">{fmtSize(f.size)}</span>
                </button>
              ))}
            </div>
            <span className="muted small">
              Zusatzdateien änderst du im Ordner <span className="mono">{detail.dir}</span> oder per erneutem Import.
            </span>
          </div>
        )}
        {preview && name && <FilePreview skill={name} file={preview} onClose={() => setPreview(null)} />}
        {error && <div className="form-error">{error}</div>}
        <div className="edit-actions">
          <button className="btn" onClick={onClose}>
            Abbrechen
          </button>
          <button className="btn primary" disabled={busy || !skillName || !description.trim() || body === null} onClick={save}>
            Speichern
          </button>
        </div>
      </div>
    </div>
  );
}

export function SkillsPage({ onAuthError }: { onAuthError: () => void }) {
  const [skills, setSkills] = useState<SkillSummary[] | null>(null);
  const [scripts, setScripts] = useState(false);
  const [editing, setEditing] = useState<string | null | "new">(null);
  const [conflict, setConflict] = useState<"rename" | "overwrite" | "skip">("rename");
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const file = useRef<HTMLInputElement>(null);

  const load = useCallback(() => {
    request<{ skills: SkillSummary[]; scripts_enabled: boolean }>("GET", "/api/skills")
      .then((r) => {
        setSkills(r.skills);
        setScripts(r.scripts_enabled);
      })
      .catch((e) => e instanceof AuthError && onAuthError());
  }, [onAuthError]);
  useEffect(load, [load]);

  const importFiles = async (files: FileList) => {
    setMessage(null);
    const done: string[] = [];
    try {
      for (const f of Array.from(files)) {
        const form = new FormData();
        form.append("file", f, f.name);
        form.append("on_conflict", conflict);
        const r = await postForm<{ imported: { name: string; action: string; original?: string }[] }>("/api/skills/import", form);
        for (const x of r.imported) {
          done.push(
            x.action === "renamed"
              ? `${x.name} (als Kopie, „${x.original}“ gab es schon)`
              : x.action === "skipped"
                ? `${x.name} übersprungen`
                : x.action === "replaced"
                  ? `${x.name} ersetzt`
                  : x.name,
          );
        }
      }
      setMessage({ ok: true, text: `Importiert: ${done.join(", ")}` });
    } catch (e: any) {
      setMessage({ ok: false, text: e.message });
    }
    load();
  };

  const active = (skills ?? []).filter((s) => s.enabled);
  const promptTokens = active.reduce((a, s) => a + Math.round((s.name.length + s.description.length + 8) / 4), 0);

  return (
    <div className="page">
      <header className="page-header">
        <h1>Skills</h1>
      </header>
      <div className="page-body">
        <div className="mem-section">
          <div className="privacy-note">
            <Sparkles size={15} />
            <span>
              Skills sind Anleitungen für wiederkehrende Aufgaben – im offenen Agent-Skills-Format (Ordner mit{" "}
              <span className="mono">SKILL.md</span>), wie bei Claude oder Codex. Im Chat steht nur Name und Beschreibung;
              die Anleitung lädt der Assistent erst, wenn sie passt.
            </span>
          </div>

          <div className="card list">
            <div className="list-title row">
              <span>Deine Skills</span>
              <span className="grow" />
              {active.length > 0 && <span className="muted small">im Kontext: ~{promptTokens} Tokens</span>}
              <button className="btn small" onClick={() => file.current?.click()}>
                <Upload size={14} /> Importieren
              </button>
              <input
                ref={file}
                type="file"
                hidden
                multiple
                accept=".zip,.skill,.md"
                onChange={(e) => {
                  if (e.target.files?.length) importFiles(e.target.files);
                  e.target.value = "";
                }}
              />
              <button
                className="btn small"
                disabled={!skills?.length}
                onClick={() => downloadFile("/api/skills/export", "skills.zip")}
                title="Alle Skills als .zip"
              >
                <Download size={14} /> Alle exportieren
              </button>
              <button className="btn small primary" onClick={() => setEditing("new")}>
                <Plus size={14} /> Neu
              </button>
            </div>
            <div className="skill-import-hint muted small">
              Import: <span className="mono">.zip</span>/<span className="mono">.skill</span> mit einem oder mehreren
              Skill-Ordnern oder eine einzelne <span className="mono">SKILL.md</span>. Bei gleichem Namen:{" "}
              <select className="select" value={conflict} onChange={(e) => setConflict(e.target.value as any)}>
                <option value="rename">als Kopie hinzufügen</option>
                <option value="overwrite">ersetzen</option>
                <option value="skip">überspringen</option>
              </select>
            </div>
            {message && (
              <div className={message.ok ? "ok-box" : "error-box"} style={{ margin: "0 14px 10px" }}>
                {message.ok && <Check size={15} />} {message.text}
              </div>
            )}
            {skills?.length === 0 && (
              <div className="pad muted">
                Noch keine Skills. Leg einen an (z. B. „So schreibe ich Status-Mails“) oder importiere fertige Skills –
                etwa aus Claude Code (<span className="mono">~/.claude/skills</span>) als .zip.
              </div>
            )}
            {skills?.map((s) => (
              <div key={s.name} className={`tool-row${s.enabled ? "" : " disabled-row"}`}>
                <Sparkles size={18} className="muted" />
                <div className="tool-row-text">
                  <div>
                    <b className="mono">{s.name}</b>{" "}
                    {s.files > 0 && <span className="badge">{s.files} {s.files === 1 ? "Datei" : "Dateien"}</span>}{" "}
                    {s.scripts > 0 && <span className="kind kind-write">{s.scripts} {s.scripts === 1 ? "Skript" : "Skripte"}</span>}
                  </div>
                  <div className="muted small skill-desc">{s.error ? <span className="err">{s.error}</span> : s.description}</div>
                </div>
                <label className="switch" title={s.enabled ? "Aktiv" : "Deaktiviert"}>
                  <input
                    type="checkbox"
                    checked={s.enabled}
                    disabled={!!s.error}
                    onChange={async (e) => {
                      await request("PUT", `/api/skills/${s.name}/enabled`, { enabled: e.target.checked });
                      load();
                    }}
                  />
                  <span />
                </label>
                <button className="icon-btn small" title="Exportieren (.zip)" onClick={() => downloadFile(`/api/skills/export?names=${s.name}`, `${s.name}.zip`)}>
                  <Download size={14} />
                </button>
                <button className="icon-btn small" title="Bearbeiten" onClick={() => setEditing(s.name)}>
                  <Pencil size={14} />
                </button>
                <button
                  className="icon-btn small"
                  title="Löschen"
                  onClick={async () => {
                    if (!confirm(`Skill „${s.name}“ mit allen Dateien löschen?`)) return;
                    await request("DELETE", `/api/skills/${s.name}`);
                    load();
                  }}
                >
                  <Trash2 size={14} />
                </button>
              </div>
            ))}
          </div>

          <div className="card">
            <div className="card-head">
              <h3>Skripte ausführen</h3>
              <span className="grow" />
              <label className="switch">
                <input
                  type="checkbox"
                  checked={scripts}
                  onChange={async (e) => {
                    const r = await request<{ scripts_enabled: boolean }>("PUT", "/api/skills-settings", { scripts_enabled: e.target.checked });
                    setScripts(r.scripts_enabled);
                  }}
                />
                <span />
              </label>
            </div>
            <p className="muted small">
              Manche Skills bringen Skripte mit (<span className="mono">scripts/…</span>, z. B. Python). Ist das an, darf der
              Assistent sie auf diesem Rechner ausführen – mit deinen Rechten, in einem eigenen Arbeitsordner pro Chat
              (dort liegen auch die hochgeladenen Dateien). Im Modus „Fragen“ bestätigst du jeden Aufruf, im Modus „Planen“
              läuft nichts.
            </p>
            {scripts && (
              <div className="warn-box small">
                <AlertTriangle size={14} /> Importiere Skills mit Skripten nur aus Quellen, denen du vertraust.
              </div>
            )}
          </div>
        </div>
      </div>
      {editing !== null && (
        <SkillEditor
          name={editing === "new" ? null : editing}
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

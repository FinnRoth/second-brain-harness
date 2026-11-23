import { AlertTriangle, BookText, Check, FileUp, KeyRound, Plug } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { request } from "../api";

interface ExportableServer {
  name: string;
  summary: string;
  secrets: string[];
  tools: number;
  policies: number;
  path_warnings: string[];
}
interface ExportableRef {
  id: number;
  title: string;
  description: string;
  chars: number;
  mode: string;
}

function download(filename: string, data: unknown) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

const slug = (s: string) =>
  s
    .toLowerCase()
    .replace(/[äöüß]/g, (c) => ({ ä: "ae", ö: "oe", ü: "ue", ß: "ss" })[c] as string)
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "") || "paket";

// ------------------------------------------------------------------- Export
export function ExportDialog({ onClose }: { onClose: () => void }) {
  const [data, setData] = useState<{ servers: ExportableServer[]; references: ExportableRef[]; custom_instructions: boolean } | null>(null);
  const [servers, setServers] = useState<Set<string>>(new Set());
  const [refs, setRefs] = useState<Set<number>>(new Set());
  const [policies, setPolicies] = useState(true);
  const [instructions, setInstructions] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    request("GET", "/api/bundles/exportable").then(setData).catch((e) => setError(e.message));
  }, []);

  const toggle = <T,>(set: Set<T>, v: T, setter: (s: Set<T>) => void) => {
    const next = new Set(set);
    next.has(v) ? next.delete(v) : next.add(v);
    setter(next);
  };
  const selected = data?.servers.filter((s) => servers.has(s.name)) ?? [];
  const secrets = Array.from(new Set(selected.flatMap((s) => s.secrets)));
  const paths = selected.filter((s) => s.path_warnings.length);
  const empty = !servers.size && !refs.size && !instructions;

  const run = async () => {
    setError(null);
    try {
      const bundle = await request("POST", "/api/bundles/export", {
        name: name || "Second-Brain-Paket",
        description,
        servers: Array.from(servers),
        references: Array.from(refs),
        tool_policies: policies,
        custom_instructions: instructions,
      });
      download(`${slug(name || "second-brain-paket")}.json`, bundle);
      onClose();
    } catch (e: any) {
      setError(e.message);
    }
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal wide bundle-dialog" onClick={(e) => e.stopPropagation()}>
        <h2>Paket exportieren</h2>
        <p className="muted small">
          Wähle aus, was zusammengehört – z. B. einen MCP-Server und das Nachschlagewerk, das seine Links beschreibt. Secrets
          werden nie exportiert, nur ihre Platzhalter.
        </p>
        <div className="form-grid">
          <label className="field">
            <span>Name</span>
            <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="z. B. SAP Interne Links" />
          </label>
          <label className="field">
            <span>Beschreibung <span className="muted">– optional</span></span>
            <input className="input" value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Wofür ist das Paket?" />
          </label>
        </div>

        <div className="pick-list">
          <div className="pick-title">MCP-Server</div>
          {data?.servers.map((s) => (
            <label key={s.name} className="pick-row">
              <input type="checkbox" checked={servers.has(s.name)} onChange={() => toggle(servers, s.name, setServers)} />
              <Plug size={15} className="muted" />
              <span className="pick-text">
                <b>{s.name}</b>
                <span className="muted small ellipsis">{s.summary}</span>
              </span>
              {s.policies > 0 && <span className="badge">{s.policies} Regeln</span>}
            </label>
          ))}
          {data && !data.servers.length && <div className="muted small pad">Keine MCP-Server konfiguriert.</div>}

          <div className="pick-title">Nachschlagewerke</div>
          {data?.references.map((r) => (
            <label key={r.id} className="pick-row">
              <input type="checkbox" checked={refs.has(r.id)} onChange={() => toggle(refs, r.id, setRefs)} />
              <BookText size={15} className="muted" />
              <span className="pick-text">
                <b>{r.title}</b>
                <span className="muted small ellipsis">{r.description || `${r.chars.toLocaleString("de-DE")} Zeichen`}</span>
              </span>
            </label>
          ))}
          {data && !data.references.length && <div className="muted small pad">Keine Nachschlagewerke.</div>}

          <div className="pick-title">Weiteres</div>
          <label className="pick-row">
            <input type="checkbox" checked={policies} onChange={(e) => setPolicies(e.target.checked)} />
            <span className="pick-text">
              <b>Tool-Regeln der gewählten Server</b>
              <span className="muted small">„Immer erlauben“, „Immer fragen“, „Blockieren“</span>
            </span>
          </label>
          {data?.custom_instructions && (
            <label className="pick-row">
              <input type="checkbox" checked={instructions} onChange={(e) => setInstructions(e.target.checked)} />
              <span className="pick-text">
                <b>Eigene Anweisungen</b>
                <span className="muted small">deine persönlichen Anweisungen aus den Einstellungen</span>
              </span>
            </label>
          )}
        </div>

        {secrets.length > 0 && (
          <div className="info-box">
            <KeyRound size={14} /> Wer importiert, muss diese Secrets selbst setzen: {secrets.map((v) => `\${${v}}`).join(", ")}
          </div>
        )}
        {paths.length > 0 && (
          <div className="warn-box small">
            <AlertTriangle size={14} /> {paths.map((p) => p.name).join(", ")} enthält Pfade deines Rechners – nach dem Import
            anpassen.
          </div>
        )}
        {error && <div className="error-box">{error}</div>}
        <div className="edit-actions">
          <button className="btn" onClick={onClose}>
            Abbrechen
          </button>
          <button className="btn primary" disabled={empty} onClick={run}>
            Exportieren
          </button>
        </div>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------- Import
interface Preview {
  name: string;
  description: string;
  servers: {
    name: string;
    exists: boolean;
    suggested_name: string;
    summary: string;
    secrets: string[];
    missing_secrets: string[];
    path_warnings: string[];
    policies: number;
  }[];
  references: { index: number; title: string; description: string; chars: number; mode: string; exists: boolean; suggested_title: string }[];
  custom_instructions: { present: boolean; chars: number; local_exists: boolean };
  missing_secrets: string[];
}

type Action = "add" | "overwrite" | "rename" | "skip";

export function ImportDialog({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [text, setText] = useState("");
  const [bundle, setBundle] = useState<any>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [serverActs, setServerActs] = useState<Record<string, { action: Action; new_name: string }>>({});
  const [refActs, setRefActs] = useState<Record<number, { action: Action; new_title: string }>>({});
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [instr, setInstr] = useState<"append" | "replace" | "skip">("skip");
  const [policies, setPolicies] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<any>(null);
  const file = useRef<HTMLInputElement>(null);

  const load = async (raw: string) => {
    setError(null);
    let data: any;
    try {
      data = JSON.parse(raw);
    } catch {
      setError("Das ist kein gültiges JSON.");
      return;
    }
    try {
      const pv = await request<Preview>("POST", "/api/bundles/preview", data);
      setBundle(data);
      setPreview(pv);
      setServerActs(
        Object.fromEntries(pv.servers.map((s) => [s.name, { action: s.exists ? "rename" : "add", new_name: s.suggested_name }])),
      );
      setRefActs(
        Object.fromEntries(pv.references.map((r) => [r.index, { action: r.exists ? "rename" : "add", new_title: r.suggested_title }])),
      );
      setInstr(pv.custom_instructions.present ? (pv.custom_instructions.local_exists ? "append" : "replace") : "skip");
    } catch (e: any) {
      setError(e.message);
    }
  };

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await request("POST", "/api/bundles/import", {
        bundle,
        decisions: {
          servers: serverActs,
          references: refActs,
          secrets,
          custom_instructions: instr,
          tool_policies: policies,
        },
      });
      setDone(r);
      onDone();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const conflictSelect = (exists: boolean, value: Action, onChange: (a: Action) => void) => (
    <select className="select" value={value} onChange={(e) => onChange(e.target.value as Action)}>
      {exists ? (
        <>
          <option value="rename">Als Kopie hinzufügen</option>
          <option value="overwrite">Vorhandenes ersetzen</option>
        </>
      ) : (
        <option value="add">Hinzufügen</option>
      )}
      <option value="skip">Überspringen</option>
    </select>
  );

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal wide bundle-dialog" onClick={(e) => e.stopPropagation()}>
        <h2>Paket importieren</h2>
        {done ? (
          <>
            <div className="ok-box">
              <Check size={16} /> Importiert: {done.result.servers.length} Server, {done.result.references.length} Nachschlagewerke
              {done.result.secrets.length > 0 && `, ${done.result.secrets.length} Secrets`}
            </div>
            {done.mcp.servers
              .filter((s: any) => done.result.servers.includes(s.name))
              .map((s: any) => (
                <div key={s.name} className="small">
                  <span className={`dot inline ${s.status}`} /> <b>{s.name}</b>: {s.status === "ready" ? `verbunden, ${s.tools.length} Tools` : s.error || s.status}
                </div>
              ))}
            <div className="edit-actions">
              <button className="btn primary" onClick={onClose}>
                Fertig
              </button>
            </div>
          </>
        ) : !preview ? (
          <>
            <p className="muted small">Eine exportierte Paket-Datei wählen oder den Inhalt einfügen. Auch reine MCP-Configs aus Claude Code funktionieren.</p>
            <div className="edit-actions" style={{ justifyContent: "flex-start" }}>
              <button className="btn" onClick={() => file.current?.click()}>
                <FileUp size={15} /> Datei wählen
              </button>
              <input
                ref={file}
                type="file"
                accept=".json,application/json"
                hidden
                onChange={async (e) => {
                  const f = e.target.files?.[0];
                  if (f) {
                    const t = await f.text();
                    setText(t);
                    load(t);
                  }
                  e.target.value = "";
                }}
              />
            </div>
            <textarea className="input mono import-area" placeholder="… oder JSON hier einfügen" value={text} onChange={(e) => setText(e.target.value)} />
            {error && <div className="error-box">{error}</div>}
            <div className="edit-actions">
              <button className="btn" onClick={onClose}>
                Abbrechen
              </button>
              <button className="btn primary" disabled={!text.trim()} onClick={() => load(text)}>
                Weiter
              </button>
            </div>
          </>
        ) : (
          <>
            <div>
              <b>{preview.name}</b>
              {preview.description && <div className="muted small">{preview.description}</div>}
            </div>
            <div className="pick-list">
              {preview.servers.length > 0 && <div className="pick-title">MCP-Server</div>}
              {preview.servers.map((s) => {
                const act = serverActs[s.name];
                return (
                  <div key={s.name} className="pick-row column">
                    <div className="pick-line">
                      <Plug size={15} className="muted" />
                      <span className="pick-text">
                        <b>{s.name}</b> {s.exists && <span className="badge">existiert bereits</span>}
                        <span className="muted small ellipsis mono">{s.summary}</span>
                      </span>
                      {conflictSelect(s.exists, act.action, (a) => setServerActs({ ...serverActs, [s.name]: { ...act, action: a } }))}
                    </div>
                    {act.action === "rename" && (
                      <input
                        className="input small-input"
                        value={act.new_name}
                        onChange={(e) => setServerActs({ ...serverActs, [s.name]: { ...act, new_name: e.target.value } })}
                      />
                    )}
                    {act.action !== "skip" && s.path_warnings.length > 0 && (
                      <div className="warn-box small">
                        <AlertTriangle size={13} /> Enthält Pfade eines anderen Rechners – danach unter „Tools & MCP“ anpassen:
                        <ul>
                          {s.path_warnings.map((w) => (
                            <li key={w} className="mono">
                              {w}
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}
                  </div>
                );
              })}
              {preview.references.length > 0 && <div className="pick-title">Nachschlagewerke</div>}
              {preview.references.map((r) => {
                const act = refActs[r.index];
                return (
                  <div key={r.index} className="pick-row column">
                    <div className="pick-line">
                      <BookText size={15} className="muted" />
                      <span className="pick-text">
                        <b>{r.title}</b> {r.exists && <span className="badge">existiert bereits</span>}
                        <span className="muted small ellipsis">{r.description || `${r.chars.toLocaleString("de-DE")} Zeichen`}</span>
                      </span>
                      {conflictSelect(r.exists, act.action, (a) => setRefActs({ ...refActs, [r.index]: { ...act, action: a } }))}
                    </div>
                    {act.action === "rename" && r.exists && (
                      <input
                        className="input small-input"
                        value={act.new_title}
                        onChange={(e) => setRefActs({ ...refActs, [r.index]: { ...act, new_title: e.target.value } })}
                      />
                    )}
                  </div>
                );
              })}
              {(preview.custom_instructions.present || preview.servers.some((s) => s.policies)) && <div className="pick-title">Weiteres</div>}
              {preview.servers.some((s) => s.policies) && (
                <label className="pick-row">
                  <input type="checkbox" checked={policies} onChange={(e) => setPolicies(e.target.checked)} />
                  <span className="pick-text">
                    <b>Tool-Regeln übernehmen</b>
                  </span>
                </label>
              )}
              {preview.custom_instructions.present && (
                <div className="pick-row">
                  <span className="pick-text">
                    <b>Eigene Anweisungen</b>
                    <span className="muted small">{preview.custom_instructions.chars.toLocaleString("de-DE")} Zeichen</span>
                  </span>
                  <select className="select" value={instr} onChange={(e) => setInstr(e.target.value as any)}>
                    {preview.custom_instructions.local_exists && <option value="append">An meine anhängen</option>}
                    <option value="replace">{preview.custom_instructions.local_exists ? "Meine ersetzen" : "Übernehmen"}</option>
                    <option value="skip">Nicht übernehmen</option>
                  </select>
                </div>
              )}
            </div>

            {preview.missing_secrets.length > 0 && (
              <div className="secrets-box">
                <div className="pick-title">
                  <KeyRound size={13} /> Fehlende Secrets
                </div>
                <p className="muted small">Kannst du jetzt eintragen oder später unter „Tools & MCP“.</p>
                {preview.missing_secrets.map((name) => (
                  <label key={name} className="secret-line">
                    <span className="mono">{"${" + name + "}"}</span>
                    <input
                      className="input"
                      type="password"
                      value={secrets[name] ?? ""}
                      onChange={(e) => setSecrets({ ...secrets, [name]: e.target.value })}
                      placeholder="Wert"
                    />
                  </label>
                ))}
              </div>
            )}
            {error && <div className="error-box">{error}</div>}
            <div className="edit-actions">
              <button className="btn" onClick={() => setPreview(null)}>
                Zurück
              </button>
              <button className="btn primary" disabled={busy} onClick={run}>
                {busy ? "Importiert …" : "Importieren"}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

import { ClipboardPaste, FileText, KeyRound, Pencil, Plus, RefreshCw, RotateCw, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { AuthError, request } from "../api";
import { MEMORY_LABELS } from "../components/ToolCard";

interface ServerStatus {
  name: string;
  transport: string;
  command: string | null;
  url: string | null;
  status: string;
  error: string | null;
  missing_vars: string[];
  restarts: number;
  tools: { name: string; read_only_hint: boolean }[];
}

interface Secret {
  name: string;
  set: boolean;
  source: string | null;
  used: boolean;
}

interface ConfigData {
  path: string;
  secrets_path: string | null;
  servers: Record<string, Record<string, any>>;
  secrets: Secret[];
}

interface Tool {
  name: string;
  title: string;
  description: string;
  source: string;
  kind: "read" | "write" | "memory";
  policy: string | null;
  override: string | null;
}

const STATUS: Record<string, string> = {
  ready: "Verbunden",
  starting: "Startet …",
  failed: "Fehler",
  disabled: "Aus",
  stopped: "Gestoppt",
};
const KIND: Record<string, string> = { read: "Lesen", write: "Verändert", memory: "Gedächtnis" };

// ------------------------------------------------------------------- Editor
type KV = { key: string; value: string };
const toKV = (o?: Record<string, string>): KV[] => Object.entries(o || {}).map(([key, value]) => ({ key, value }));
const fromKV = (rows: KV[]) =>
  Object.fromEntries(rows.filter((r) => r.key.trim()).map((r) => [r.key.trim(), r.value])) as Record<string, string>;
const toList = (s: string) =>
  s
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean);

const TEMPLATES: { label: string; config: Record<string, any> }[] = [
  { label: "npm-Paket (npx)", config: { command: "npx", args: ["-y", "@scope/mcp-server"] } },
  {
    label: "Python aus Git (uvx)",
    config: { command: "uvx", args: ["--from", "git+https://github.com/USER/REPO", "server-befehl"] },
  },
  { label: "Lokaler Ordner (uv)", config: { command: "uv", args: ["run", "--directory", "~/code/mein-mcp", "server.py"] } },
  { label: "Remote (HTTP)", config: { type: "http", url: "https://example.com/mcp" } },
];

function KVEditor({ rows, onChange, keyLabel, valueLabel }: { rows: KV[]; onChange: (r: KV[]) => void; keyLabel: string; valueLabel: string }) {
  return (
    <div className="kv-editor">
      {rows.map((r, i) => (
        <div key={i} className="kv-row">
          <input
            className="input mono"
            placeholder={keyLabel}
            value={r.key}
            onChange={(e) => onChange(rows.map((x, j) => (j === i ? { ...x, key: e.target.value } : x)))}
          />
          <input
            className="input mono"
            placeholder={valueLabel}
            value={r.value}
            onChange={(e) => onChange(rows.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))}
          />
          <button className="icon-btn small" title="Entfernen" onClick={() => onChange(rows.filter((_, j) => j !== i))}>
            <X size={14} />
          </button>
        </div>
      ))}
      <button className="btn small ghost" onClick={() => onChange([...rows, { key: "", value: "" }])}>
        <Plus size={13} /> Hinzufügen
      </button>
    </div>
  );
}

function ServerEditor({
  name: initialName,
  config: initial,
  tools,
  onClose,
  onSaved,
}: {
  name: string | null;
  config: Record<string, any>;
  tools: string[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState(initialName ?? "");
  const [base, setBase] = useState<Record<string, any>>(initial);
  const transportOf = (c: Record<string, any>) => {
    const t = String(c.type || c.transport || (c.command ? "stdio" : c.url ? "http" : "stdio")).toLowerCase();
    return t.includes("http") ? "http" : t === "sse" ? "sse" : "stdio";
  };
  const [transport, setTransport] = useState(transportOf(initial));
  const [command, setCommand] = useState(initial.command ?? "");
  const [args, setArgs] = useState((initial.args ?? []).join("\n"));
  const [cwd, setCwd] = useState(initial.cwd ?? "");
  const [env, setEnv] = useState<KV[]>(toKV(initial.env));
  const [url, setUrl] = useState(initial.url ?? "");
  const [headers, setHeaders] = useState<KV[]>(toKV(initial.headers));
  const [timeout, setTimeout_] = useState(String(initial.timeout ?? ""));
  const [enabled, setEnabled] = useState(!initial.disabled && initial.enabled !== false);
  const rules = initial.tools ?? {};
  const [readOnly, setReadOnly] = useState((rules.readOnly ?? rules.read_only ?? []).join(", "));
  const [ask, setAsk] = useState((rules.ask ?? []).join(", "));
  const [allow, setAllow] = useState((rules.allow ?? []).join(", "));
  const [deny, setDeny] = useState((rules.deny ?? []).join(", "));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const applyTemplate = (c: Record<string, any>) => {
    setBase({});
    setTransport(transportOf(c));
    setCommand(c.command ?? "");
    setArgs((c.args ?? []).join("\n"));
    setUrl(c.url ?? "");
  };

  const save = async () => {
    setBusy(true);
    setError(null);
    const { type: _t, transport: _tr, command: _c, args: _a, cwd: _w, env: _e, url: _u, headers: _h, timeout: _to, disabled: _d, enabled: _en, tools: _tools, ...extra } = base;
    const config: Record<string, any> = { ...extra };
    if (transport === "stdio") {
      config.command = command.trim();
      config.args = args.split("\n").map((a: string) => a.trim()).filter(Boolean);
      if (cwd.trim()) config.cwd = cwd.trim();
      config.env = fromKV(env);
    } else {
      config.type = transport;
      config.url = url.trim();
      config.headers = fromKV(headers);
    }
    if (timeout.trim()) config.timeout = Number(timeout);
    if (!enabled) config.disabled = true;
    config.tools = { readOnly: toList(readOnly), ask: toList(ask), allow: toList(allow), deny: toList(deny) };
    try {
      await request("PUT", "/mcp/config/servers", { name: name.trim(), previous_name: initialName, config });
      onSaved();
    } catch (e: any) {
      setError(e.message);
      setBusy(false);
    }
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal wide server-editor" onClick={(e) => e.stopPropagation()}>
        <div className="card-head">
          <h2>{initialName ? `${initialName} bearbeiten` : "MCP-Server hinzufügen"}</h2>
          <span className="grow" />
          <label className="toggle small">
            <span className="switch">
              <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
              <span />
            </span>
            Aktiv
          </label>
        </div>
        {!initialName && (
          <div className="templates">
            <span className="muted small">Vorlage:</span>
            {TEMPLATES.map((t) => (
              <button key={t.label} className="chip" onClick={() => applyTemplate(t.config)}>
                {t.label}
              </button>
            ))}
          </div>
        )}
        <div className="form-grid">
          <label className="field">
            <span>Name</span>
            <input className="input" value={name} autoFocus={!initialName} onChange={(e) => setName(e.target.value)} placeholder="z. B. sap-intern" />
          </label>
          <div className="field">
            <span>Verbindung</span>
            <div className="segmented">
              <button className={transport === "stdio" ? "active" : ""} onClick={() => setTransport("stdio")}>
                Befehl (lokal)
              </button>
              <button className={transport === "http" ? "active" : ""} onClick={() => setTransport("http")}>
                HTTP
              </button>
              <button className={transport === "sse" ? "active" : ""} onClick={() => setTransport("sse")}>
                SSE
              </button>
            </div>
          </div>
        </div>

        {transport === "stdio" ? (
          <>
            <label className="field">
              <span>Befehl</span>
              <input className="input mono" value={command} onChange={(e) => setCommand(e.target.value)} placeholder="npx, uvx, uv, node, python …" />
            </label>
            <label className="field">
              <span>Argumente <span className="muted">– eines pro Zeile</span></span>
              <textarea className="input mono" rows={4} value={args} onChange={(e) => setArgs(e.target.value)} placeholder={"-y\n@scope/mcp-server"} />
            </label>
            <label className="field">
              <span>Arbeitsverzeichnis <span className="muted">– optional</span></span>
              <input className="input mono" value={cwd} onChange={(e) => setCwd(e.target.value)} placeholder="~/code/mein-mcp" />
            </label>
            <div className="field">
              <span>Umgebungsvariablen</span>
              <KVEditor rows={env} onChange={setEnv} keyLabel="NAME" valueLabel="Wert oder ${SECRET_NAME}" />
            </div>
          </>
        ) : (
          <>
            <label className="field">
              <span>URL</span>
              <input className="input mono" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://…/mcp" />
            </label>
            <div className="field">
              <span>Header</span>
              <KVEditor rows={headers} onChange={setHeaders} keyLabel="Authorization" valueLabel="Bearer ${TOKEN}" />
            </div>
          </>
        )}
        <p className="muted small">
          Für Tokens und Passwörter <code>{"${NAME}"}</code> eintragen und den Wert unten unter „Secrets“ hinterlegen –
          so landet er nicht in <code>mcp.json</code>.
        </p>

        <details className="advanced">
          <summary>Erweitert: Timeout & Tool-Regeln</summary>
          <div className="form-grid">
            <label className="field">
              <span>Timeout pro Aufruf (Sekunden)</span>
              <input className="input" inputMode="numeric" value={timeout} onChange={(e) => setTimeout_(e.target.value)} placeholder="60" />
            </label>
            <label className="field">
              <span>Gelten als lesend</span>
              <input className="input mono" value={readOnly} onChange={(e) => setReadOnly(e.target.value)} placeholder="get_*, search" />
            </label>
            <label className="field">
              <span>Immer fragen</span>
              <input className="input mono" value={ask} onChange={(e) => setAsk(e.target.value)} />
            </label>
            <label className="field">
              <span>Immer erlauben</span>
              <input className="input mono" value={allow} onChange={(e) => setAllow(e.target.value)} />
            </label>
            <label className="field">
              <span>Blockieren</span>
              <input className="input mono" value={deny} onChange={(e) => setDeny(e.target.value)} />
            </label>
          </div>
          <p className="muted small">
            Tool-Namen kommagetrennt, <code>*</code> als Platzhalter.
            {tools.length > 0 && <> Verfügbar: {tools.join(", ")}</>}
          </p>
        </details>

        {error && <div className="error-box">{error}</div>}
        <div className="edit-actions">
          <button className="btn" onClick={onClose}>
            Abbrechen
          </button>
          <button className="btn primary" disabled={busy || !name.trim()} onClick={save}>
            {busy ? "Startet …" : "Speichern & starten"}
          </button>
        </div>
      </div>
    </div>
  );
}

function ImportModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setError(null);
    let data: any;
    try {
      data = JSON.parse(text);
    } catch {
      setError("Das ist kein gültiges JSON.");
      return;
    }
    setBusy(true);
    try {
      await request("POST", "/mcp/config/import", data);
      onDone();
    } catch (e: any) {
      setError(e.message);
      setBusy(false);
    }
  };
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal wide" onClick={(e) => e.stopPropagation()}>
        <h2>JSON einfügen</h2>
        <p className="muted small">
          Konfiguration aus Claude Code, Claude Desktop oder einer README einfügen, z. B.{" "}
          <code>{'{"mcpServers": {"name": {"command": "npx", "args": [...]}}}'}</code>
        </p>
        <textarea className="input mono import-area" autoFocus value={text} onChange={(e) => setText(e.target.value)} />
        {error && <div className="error-box">{error}</div>}
        <div className="edit-actions">
          <button className="btn" onClick={onClose}>
            Abbrechen
          </button>
          <button className="btn primary" disabled={busy || !text.trim()} onClick={submit}>
            Übernehmen
          </button>
        </div>
      </div>
    </div>
  );
}

function Secrets({ secrets, onChanged }: { secrets: Secret[]; onChanged: () => void }) {
  const [editing, setEditing] = useState<string | null>(null);
  const [value, setValue] = useState("");
  const [newName, setNewName] = useState("");
  const [error, setError] = useState<string | null>(null);

  const save = async (name: string, v: string) => {
    setError(null);
    try {
      await request("PUT", `/mcp/secrets/${encodeURIComponent(name)}`, { value: v });
      setEditing(null);
      setValue("");
      setNewName("");
      onChanged();
    } catch (e: any) {
      setError(e.message);
    }
  };

  return (
    <div className="card list">
      <div className="list-title row">
        <KeyRound size={14} /> <span>Secrets</span>
        <span className="grow" />
        <span className="muted small">Werte werden nie angezeigt</span>
      </div>
      {secrets.map((s) => (
        <div key={s.name} className="tool-row">
          <div className="tool-row-text">
            <div className="mono">{"${" + s.name + "}"}</div>
            <div className="small">
              {s.set ? (
                <span className="ok">{s.source === "umgebung" ? "aus Umgebungsvariable" : "gesetzt"}</span>
              ) : (
                <span className="err">fehlt</span>
              )}
              {!s.used && <span className="muted"> · wird nicht verwendet</span>}
            </div>
          </div>
          {editing === s.name ? (
            <form
              className="secret-form"
              onSubmit={(e) => {
                e.preventDefault();
                save(s.name, value);
              }}
            >
              <input className="input" type="password" autoFocus value={value} onChange={(e) => setValue(e.target.value)} placeholder="Wert" />
              <button className="btn small primary" disabled={!value}>
                Speichern
              </button>
              <button type="button" className="icon-btn small" onClick={() => setEditing(null)}>
                <X size={14} />
              </button>
            </form>
          ) : (
            <>
              <button className="btn small" onClick={() => (setEditing(s.name), setValue(""))}>
                {s.set && s.source === "secrets" ? "Ändern" : "Setzen"}
              </button>
              {s.source === "secrets" && (
                <button
                  className="icon-btn small"
                  title="Löschen"
                  onClick={async () => {
                    await request("DELETE", `/mcp/secrets/${encodeURIComponent(s.name)}`);
                    onChanged();
                  }}
                >
                  <Trash2 size={14} />
                </button>
              )}
            </>
          )}
        </div>
      ))}
      <form
        className="tool-row secret-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (newName.trim() && value) save(newName.trim(), value);
        }}
      >
        <input className="input mono" placeholder="NEUER_NAME" value={editing ? "" : newName} disabled={!!editing} onChange={(e) => setNewName(e.target.value.toUpperCase().replace(/[^A-Z0-9_]/g, "_"))} />
        <input className="input" type="password" placeholder="Wert" value={editing ? "" : value} disabled={!!editing} onChange={(e) => setValue(e.target.value)} />
        <button className="btn small" disabled={!!editing || !newName.trim() || !value}>
          <Plus size={13} /> Hinzufügen
        </button>
      </form>
      {error && <div className="error-box small">{error}</div>}
    </div>
  );
}

// --------------------------------------------------------------------- Seite
export function ToolsPage({ onAuthError }: { onAuthError: () => void }) {
  const [status, setStatus] = useState<{ config_error: string | null; servers: ServerStatus[] } | null>(null);
  const [config, setConfig] = useState<ConfigData | null>(null);
  const [tools, setTools] = useState<Tool[]>([]);
  const [logs, setLogs] = useState<{ name: string; log: string } | null>(null);
  const [editor, setEditor] = useState<{ name: string | null; config: Record<string, any> } | null>(null);
  const [importing, setImporting] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    Promise.all([request("GET", "/mcp/servers"), request<ConfigData>("GET", "/mcp/config"), request<Tool[]>("GET", "/api/tools")])
      .then(([s, c, t]) => {
        setStatus(s);
        setConfig(c);
        setTools(t);
        setError(null);
      })
      .catch((e) => (e instanceof AuthError ? onAuthError() : setError(e.message)));
  }, [onAuthError]);

  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [load]);

  const act = async (key: string, fn: () => Promise<unknown>) => {
    setBusy(key);
    setError(null);
    try {
      await fn();
      load();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(null);
    }
  };

  const servers = useMemo(() => {
    const st = new Map((status?.servers ?? []).map((s) => [s.name, s]));
    return Object.entries(config?.servers ?? {}).map(([name, cfg]) => ({ name, cfg, st: st.get(name) }));
  }, [status, config]);

  const grouped = useMemo(() => {
    const g: Record<string, Tool[]> = {};
    for (const t of tools) (g[t.source] ||= []).push(t);
    const order = (s: string) => (s === "gedächtnis" ? 0 : s === "nachschlagewerke" ? 1 : 2);
    return Object.entries(g).sort(([a], [b]) => order(a) - order(b) || a.localeCompare(b));
  }, [tools]);

  return (
    <div className="page">
      <header className="page-header">
        <h1>Tools & MCP</h1>
        <span className="grow" />
        <button className="btn small" onClick={() => setImporting(true)}>
          <ClipboardPaste size={14} /> JSON einfügen
        </button>
        <button className="btn small primary" onClick={() => setEditor({ name: null, config: {} })}>
          <Plus size={14} /> Server hinzufügen
        </button>
      </header>
      <div className="page-body">
        <div className="mem-section">
          {error && <div className="error-banner">{error}</div>}
          {status?.config_error && <div className="error-box">{status.config_error}</div>}

          <div className="server-grid">
            {servers.map(({ name, cfg, st }) => {
              const enabled = !cfg.disabled && cfg.enabled !== false;
              const s = st?.status ?? (enabled ? "starting" : "disabled");
              return (
                <div key={name} className={`card server${enabled ? "" : " disabled-row"}`}>
                  <div className="card-head">
                    <span className={`dot ${s}`} />
                    <h3>{name}</h3>
                    <span className="muted small">{STATUS[s] ?? s}</span>
                    <span className="grow" />
                    <label className="switch" title={enabled ? "Ausschalten" : "Einschalten"}>
                      <input
                        type="checkbox"
                        checked={enabled}
                        disabled={busy === name}
                        onChange={(e) => {
                          const { disabled: _d, enabled: _e, ...rest } = cfg;
                          act(name, () =>
                            request("PUT", "/mcp/config/servers", {
                              name,
                              config: e.target.checked ? rest : { ...rest, disabled: true },
                            }),
                          );
                        }}
                      />
                      <span />
                    </label>
                  </div>
                  <div className="mono small muted ellipsis" title={st?.command || st?.url || cfg.command || cfg.url}>
                    {st?.command || st?.url || [cfg.command, ...(cfg.args ?? [])].join(" ") || cfg.url}
                  </div>
                  <div className="server-foot">
                    <span className="small">
                      {st?.tools.length ?? 0} Tools
                      {st && st.restarts > 0 && ` · ${st.restarts}× neu gestartet`}
                    </span>
                    <span className="grow" />
                    <button className="icon-btn small" title="Bearbeiten" onClick={() => setEditor({ name, config: cfg })}>
                      <Pencil size={14} />
                    </button>
                    <button
                      className="icon-btn small"
                      title="Logs"
                      onClick={async () => setLogs(await request("GET", `/mcp/servers/${name}/logs?lines=200`))}
                    >
                      <FileText size={14} />
                    </button>
                    <button
                      className="icon-btn small"
                      title="Neu starten"
                      disabled={!enabled || busy === name}
                      onClick={() => act(name, () => request("POST", `/mcp/servers/${name}/restart`))}
                    >
                      <RotateCw size={14} className={busy === name ? "spin" : ""} />
                    </button>
                    <button
                      className="icon-btn small"
                      title="Entfernen"
                      onClick={() => {
                        if (window.confirm(`MCP-Server „${name}“ entfernen?`))
                          act(name, () => request("DELETE", `/mcp/config/servers/${encodeURIComponent(name)}`));
                      }}
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                  {st?.error && <div className="error-box small">{st.error}</div>}
                  {st && st.missing_vars.length > 0 && (
                    <div className="warn-box small">Secret fehlt: {st.missing_vars.map((v) => `\${${v}}`).join(", ")}</div>
                  )}
                </div>
              );
            })}
            {config && !servers.length && (
              <div className="card muted">
                Noch keine MCP-Server. Füge einen hinzu oder kopiere die Konfiguration aus Claude Code per „JSON einfügen“.
              </div>
            )}
          </div>
          {config && (
            <p className="muted small">
              Gespeichert in <code>{config.path}</code> – die Datei kannst du weiterhin auch von Hand bearbeiten.{" "}
              <button className="link-btn" onClick={() => act("reload", () => request("POST", "/mcp/reload"))}>
                <RefreshCw size={12} className={busy === "reload" ? "spin" : ""} /> Neu laden
              </button>
            </p>
          )}

          {config && <Secrets secrets={config.secrets} onChanged={load} />}

          <h2 className="section-title">Freigaben</h2>
          <p className="muted">
            Wie Tools behandelt werden, hängt vom Modus im Chat ab (Planen / Fragen / Auto). Hier kannst du einzelne Tools
            fest einstellen.
          </p>
          {grouped.map(([source, list]) => (
            <div key={source} className="card list">
              <div className="list-title">
                {source === "gedächtnis" ? "Gedächtnis" : source === "nachschlagewerke" ? "Nachschlagewerke" : source.replace("mcp:", "MCP · ")}
              </div>
              {list.map((t) => (
                <div key={t.name} className="tool-row">
                  <div className="tool-row-text">
                    <div>
                      <b>{t.source.startsWith("mcp:") ? t.title : (MEMORY_LABELS[t.name] ?? t.title)}</b>{" "}
                      <span className={`kind kind-${t.kind}`}>{KIND[t.kind]}</span>
                    </div>
                    <div className="muted small ellipsis" title={t.description}>
                      {t.description.replace(/^\[MCP: [^\]]+\]\s*/, "")}
                    </div>
                  </div>
                  {t.kind === "memory" ? (
                    <span className="muted small">immer erlaubt</span>
                  ) : (
                    <select
                      className="select"
                      value={t.override ?? ""}
                      onChange={(e) =>
                        act(t.name, () =>
                          request("PUT", `/api/tools/${encodeURIComponent(t.name)}/policy`, { policy: e.target.value || null }),
                        )
                      }
                    >
                      <option value="">Nach Modus{t.policy && !t.override ? ` (Config: ${t.policy})` : ""}</option>
                      <option value="allow">Immer erlauben</option>
                      <option value="ask">Immer fragen</option>
                      <option value="deny">Blockieren</option>
                    </select>
                  )}
                </div>
              ))}
            </div>
          ))}
        </div>
      </div>

      {editor && (
        <ServerEditor
          name={editor.name}
          config={editor.config}
          tools={(status?.servers.find((s) => s.name === editor.name)?.tools ?? []).map((t) => t.name)}
          onClose={() => setEditor(null)}
          onSaved={() => {
            setEditor(null);
            load();
          }}
        />
      )}
      {importing && (
        <ImportModal
          onClose={() => setImporting(false)}
          onDone={() => {
            setImporting(false);
            load();
          }}
        />
      )}
      {logs && (
        <div className="modal-backdrop" onClick={() => setLogs(null)}>
          <div className="modal wide" onClick={(e) => e.stopPropagation()}>
            <div className="card-head">
              <h3>Logs: {logs.name}</h3>
              <span className="grow" />
              <button className="icon-btn" onClick={() => setLogs(null)}>
                <X size={18} />
              </button>
            </div>
            <pre className="log">{logs.log || "(leer)"}</pre>
          </div>
        </div>
      )}
    </div>
  );
}

import { FileText, RefreshCw, RotateCw, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { AuthError, request } from "../api";
import { MEMORY_LABELS } from "../components/ToolCard";

interface Server {
  name: string;
  transport: string;
  command: string | null;
  url: string | null;
  status: string;
  error: string | null;
  missing_vars: string[];
  restarts: number;
  tools: { name: string }[];
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
  disabled: "Deaktiviert",
  stopped: "Gestoppt",
};

const KIND: Record<string, string> = { read: "Lesen", write: "Verändert", memory: "Gedächtnis" };

export function ToolsPage({ onAuthError }: { onAuthError: () => void }) {
  const [servers, setServers] = useState<{ config_path: string | null; config_error: string | null; servers: Server[] } | null>(null);
  const [tools, setTools] = useState<Tool[]>([]);
  const [logs, setLogs] = useState<{ name: string; log: string } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    Promise.all([request("GET", "/mcp/servers"), request<Tool[]>("GET", "/api/tools")])
      .then(([s, t]) => {
        setServers(s);
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

  const grouped = useMemo(() => {
    const g: Record<string, Tool[]> = {};
    for (const t of tools) (g[t.source] ||= []).push(t);
    return Object.entries(g).sort(([a], [b]) => (a === "gedächtnis" ? -1 : b === "gedächtnis" ? 1 : a.localeCompare(b)));
  }, [tools]);

  const act = async (key: string, fn: () => Promise<unknown>) => {
    setBusy(key);
    try {
      await fn();
      load();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="page">
      <header className="page-header">
        <h1>Tools & MCP</h1>
        <span className="grow" />
        <button className="btn small" disabled={busy === "reload"} onClick={() => act("reload", () => request("POST", "/mcp/reload"))}>
          <RefreshCw size={14} className={busy === "reload" ? "spin" : ""} /> Config neu laden
        </button>
      </header>
      <div className="page-body">
        {error && <div className="error-banner">{error}</div>}
        <div className="mem-section">
          <p className="muted">
            MCP-Server werden in <code>{servers?.config_path || "config/mcp.json"}</code> eingetragen (Format wie bei
            Claude Code). Änderungen werden automatisch übernommen.
          </p>
          {servers?.config_error && <div className="error-box">{servers.config_error}</div>}
          <div className="server-grid">
            {servers?.servers.map((s) => (
              <div key={s.name} className="card server">
                <div className="card-head">
                  <span className={`dot ${s.status}`} />
                  <h3>{s.name}</h3>
                  <span className="muted small">{STATUS[s.status] ?? s.status}</span>
                  <span className="grow" />
                  <button
                    className="icon-btn small"
                    title="Logs"
                    onClick={async () => setLogs(await request("GET", `/mcp/servers/${s.name}/logs?lines=200`))}
                  >
                    <FileText size={15} />
                  </button>
                  <button
                    className="icon-btn small"
                    title="Neu starten"
                    disabled={busy === s.name}
                    onClick={() => act(s.name, () => request("POST", `/mcp/servers/${s.name}/restart`))}
                  >
                    <RotateCw size={15} className={busy === s.name ? "spin" : ""} />
                  </button>
                </div>
                <div className="mono small muted ellipsis">{s.command || s.url}</div>
                <div className="small">
                  {s.tools.length} Tools · {s.transport}
                  {s.restarts > 0 && ` · ${s.restarts}× neu gestartet`}
                </div>
                {s.error && <div className="error-box small">{s.error}</div>}
                {s.missing_vars.length > 0 && (
                  <div className="warn-box small">Nicht gesetzt: {s.missing_vars.map((v) => `\${${v}}`).join(", ")}</div>
                )}
              </div>
            ))}
            {servers && !servers.servers.length && (
              <div className="card muted">
                Noch keine MCP-Server eingerichtet. Beispiele findest du in <code>config/mcp.example.json</code>.
              </div>
            )}
          </div>

          <h2 className="section-title">Freigaben</h2>
          <p className="muted">
            Wie Tools behandelt werden, hängt vom Modus im Chat ab (Planen / Fragen / Auto). Hier kannst du einzelne Tools
            fest einstellen.
          </p>
          {grouped.map(([source, list]) => (
            <div key={source} className="card list">
              <div className="list-title">{source === "gedächtnis" ? "Gedächtnis" : source === "nachschlagewerke" ? "Nachschlagewerke" : source.replace("mcp:", "MCP · ")}</div>
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
                          request("PUT", `/api/tools/${encodeURIComponent(t.name)}/policy`, {
                            policy: e.target.value || null,
                          }),
                        )
                      }
                    >
                      <option value="">
                        Nach Modus{t.policy && !t.override ? ` (Config: ${t.policy})` : ""}
                      </option>
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

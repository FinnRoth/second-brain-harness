import {
  Ban,
  BookOpen,
  BookText,
  Brain,
  Check,
  ChevronRight,
  Loader2,
  PauseCircle,
  Plug,
  Search,
  ShieldQuestion,
  X,
} from "lucide-react";
import { useState } from "react";
import { api } from "../api";
import type { ToolPart } from "../types";

export const MEMORY_LABELS: Record<string, string> = {
  search_memory: "Gedächtnis durchsucht",
  get_entity: "Steckbrief geladen",
  list_entities: "Liste geladen",
  search_past_chats: "Frühere Chats durchsucht",
  remember: "Gemerkt",
  forget_fact: "Vergessen",
  update_entity: "Eintrag aktualisiert",
  tool_search: "Tools gesucht",
  read_reference: "Nachgeschlagen",
  read_document: "Dokument gelesen",
  use_skill: "Skill geladen",
  read_skill_file: "Skill-Datei gelesen",
  run_skill_script: "Skript ausgeführt",
};

const RUNNING_LABELS: Record<string, string> = {
  search_memory: "Durchsucht Gedächtnis",
  get_entity: "Lädt Steckbrief",
  list_entities: "Lädt Liste",
  search_past_chats: "Durchsucht frühere Chats",
  remember: "Merkt sich",
  forget_fact: "Vergisst",
  update_entity: "Aktualisiert Eintrag",
  tool_search: "Sucht Tools",
  read_reference: "Schlägt nach",
  read_document: "Liest Dokument",
  use_skill: "Lädt Skill",
  read_skill_file: "Liest Skill-Datei",
  run_skill_script: "Führt Skript aus",
};

export function argSummary(part: ToolPart, max = 70): string {
  const a = part.args as Record<string, any>;
  const v = a.script ?? a.query ?? a.name ?? a.content ?? a.text ?? a.path ?? a.url ?? a.fact_id;
  if (typeof v === "string" || typeof v === "number") {
    const s = String(v);
    return s.length > max ? s.slice(0, max - 1) + "…" : s;
  }
  const keys = Object.keys(a);
  if (!keys.length) return "";
  const s = JSON.stringify(a);
  return s.length > max ? s.slice(0, max - 1) + "…" : s;
}

export function toolLabel(part: ToolPart): { label: string; server: string | null } {
  if (part.source.startsWith("mcp:")) return { label: part.title || part.name, server: part.source.slice(4) };
  const running = part.status === "running" || part.status === "approval";
  const label = (running ? RUNNING_LABELS : MEMORY_LABELS)[part.name] ?? part.title ?? part.name;
  return { label, server: null };
}

export function StatusIcon({ status }: { status: ToolPart["status"] }) {
  if (status === "running") return <Loader2 size={13} className="spin muted" />;
  if (status === "approval") return <PauseCircle size={13} className="warn" />;
  if (status === "done") return <Check size={13} className="ok" />;
  if (status === "denied") return <Ban size={13} className="muted" />;
  return <X size={13} className="err" />;
}

export function SourceIcon({ part, size = 14 }: { part: ToolPart; size?: number }) {
  if (part.name === "tool_search") return <Search size={size} />;
  if (part.name === "read_reference") return <BookText size={size} />;
  if (part.source.startsWith("mcp:")) return <Plug size={size} />;
  if (part.name === "remember" || part.name === "forget_fact" || part.name === "update_entity")
    return <Brain size={size} />;
  return <BookOpen size={size} />;
}

/** Eine Tool-Nutzung als kompakte Zeile; Details aufklappbar. */
export function ToolLine({ part }: { part: ToolPart }) {
  const [open, setOpen] = useState(false);
  const { label, server } = toolLabel(part);
  const arg = argSummary(part);
  return (
    <div className={`tool-line status-${part.status}${open ? " open" : ""}`}>
      <button className="tool-line-head" onClick={() => setOpen(!open)} title="Details anzeigen">
        <span className="tool-line-icon">
          <SourceIcon part={part} />
        </span>
        <span className="tool-line-text">
          {server && <span className="tool-server">{server} · </span>}
          <span className="tool-name">{label}</span>
          {arg && <span className="tool-arg"> {arg}</span>}
        </span>
        <StatusIcon status={part.status} />
        <ChevronRight size={13} className={`chev${open ? " open" : ""}`} />
      </button>
      {open && (
        <div className="tool-details">
          {Object.keys(part.args).length > 0 && <pre>{JSON.stringify(part.args, null, 2)}</pre>}
          {part.result !== undefined && <pre className="tool-result">{part.result}</pre>}
          {part.result === undefined && part.status === "running" && <div className="muted small">Läuft …</div>}
        </div>
      )}
    </div>
  );
}

/** Freigabe-Anfrage: kompakt, aber deutlich. */
export function ApprovalCard({ part, live }: { part: ToolPart; live: boolean }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [showArgs, setShowArgs] = useState(false);
  const { label, server } = toolLabel({ ...part, status: "done" });
  const arg = argSummary(part, 120);

  if (!live || !part.approval_id) {
    return (
      <div className="tool-line status-denied">
        <div className="tool-line-head static">
          <span className="tool-line-icon">
            <PauseCircle size={14} />
          </span>
          <span className="tool-line-text">
            <span className="tool-name">{server ? `${server} · ${label}` : label}</span>
            <span className="tool-arg"> – nicht freigegeben</span>
          </span>
        </div>
      </div>
    );
  }

  const decide = async (decision: "allow" | "deny", scope: "once" | "chat" | "always" = "once") => {
    setBusy(true);
    setErr(null);
    try {
      await api.approve(part.approval_id!, decision, scope);
    } catch (e: any) {
      setErr(e.message);
      setBusy(false);
    }
  };

  return (
    <div className="approval-card">
      <div className="approval-top">
        <ShieldQuestion size={16} className="approval-icon" />
        <div className="approval-text">
          <div className="approval-title">
            Darf ich <b>{label}</b>
            {server && <span className="muted"> ({server})</span>} ausführen?
          </div>
          {arg && (
            <button className="approval-arg" onClick={() => setShowArgs(!showArgs)} title="Alle Parameter anzeigen">
              {arg}
              <ChevronRight size={12} className={`chev${showArgs ? " open" : ""}`} />
            </button>
          )}
        </div>
      </div>
      {showArgs && <pre className="approval-args">{JSON.stringify(part.args, null, 2)}</pre>}
      <div className="approval-actions">
        <button className="btn small primary" disabled={busy} onClick={() => decide("allow")} autoFocus>
          Erlauben
        </button>
        <button className="btn small" disabled={busy} onClick={() => decide("allow", "chat")}>
          Für diesen Chat
        </button>
        <button className="btn small" disabled={busy} onClick={() => decide("allow", "always")}>
          Immer
        </button>
        <span className="grow" />
        <button className="btn small danger-ghost" disabled={busy} onClick={() => decide("deny")}>
          Ablehnen
        </button>
      </div>
      {err && <div className="form-error">{err}</div>}
    </div>
  );
}

/** Mehrere Tool-Nutzungen hintereinander → eine einklappbare Gruppe. */
export function ToolGroup({ parts, live }: { parts: ToolPart[]; live: boolean }) {
  const running = parts.some((p) => p.status === "running");
  const [open, setOpen] = useState(false);
  const pending = parts.filter((p) => p.status === "approval");
  const rest = parts.filter((p) => p.status !== "approval" || !live);

  if (parts.length === 1 && !pending.length) return <ToolLine part={parts[0]} />;

  const failed = rest.filter((p) => p.status === "error").length;
  const last = rest[rest.length - 1];
  const sources = Array.from(new Set(rest.map((p) => (p.source.startsWith("mcp:") ? p.source.slice(4) : "Gedächtnis"))));

  return (
    <div className="tool-group">
      {rest.length > 1 ? (
        <>
          <button className={`tool-line-head group${open ? " open" : ""}`} onClick={() => setOpen(!open)}>
            <span className="tool-line-icon">{last && <SourceIcon part={last} />}</span>
            <span className="tool-line-text">
              <span className="tool-name">
                {running && live && last ? toolLabel(last).label : `${rest.length} Aktionen`}
              </span>
              <span className="tool-arg"> · {sources.join(", ")}</span>
              {failed > 0 && <span className="err"> · {failed} fehlgeschlagen</span>}
            </span>
            {running && live ? <Loader2 size={13} className="spin muted" /> : <Check size={13} className={failed ? "warn" : "ok"} />}
            <ChevronRight size={13} className={`chev${open ? " open" : ""}`} />
          </button>
          {open && (
            <div className="tool-group-list">
              {rest.map((p) => (
                <ToolLine key={p.call_id} part={p} />
              ))}
            </div>
          )}
        </>
      ) : (
        rest.map((p) => <ToolLine key={p.call_id} part={p} />)
      )}
      {live && pending.map((p) => <ApprovalCard key={p.call_id} part={p} live={live} />)}
    </div>
  );
}

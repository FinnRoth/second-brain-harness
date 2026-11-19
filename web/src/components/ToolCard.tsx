import {
  Ban,
  BookOpen,
  Brain,
  Check,
  ChevronRight,
  Loader2,
  PauseCircle,
  Plug,
  Search,
  ShieldCheck,
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
};

function summary(part: ToolPart): string {
  const a = part.args as Record<string, any>;
  const v = a.query ?? a.name ?? a.content ?? a.text ?? a.path ?? a.fact_id;
  if (typeof v === "string" || typeof v === "number") return String(v);
  const keys = Object.keys(a);
  if (!keys.length) return "";
  const s = JSON.stringify(a);
  return s.length > 80 ? s.slice(0, 79) + "…" : s;
}

export function toolLabel(part: ToolPart): { label: string; server: string | null } {
  if (part.source.startsWith("mcp:")) return { label: part.title || part.name, server: part.source.slice(4) };
  return { label: MEMORY_LABELS[part.name] ?? part.title ?? part.name, server: null };
}

function StatusIcon({ status }: { status: ToolPart["status"] }) {
  if (status === "running") return <Loader2 size={15} className="spin" />;
  if (status === "approval") return <PauseCircle size={15} className="warn" />;
  if (status === "done") return <Check size={15} className="ok" />;
  if (status === "denied") return <Ban size={15} className="muted" />;
  return <X size={15} className="err" />;
}

function SourceIcon({ part }: { part: ToolPart }) {
  if (part.name === "tool_search") return <Search size={15} />;
  if (part.source.startsWith("mcp:")) return <Plug size={15} />;
  if (part.name === "remember" || part.name === "forget_fact" || part.name === "update_entity")
    return <Brain size={15} />;
  return <BookOpen size={15} />;
}

export function ToolCard({ part, live }: { part: ToolPart; live: boolean }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const { label, server } = toolLabel(part);
  const awaiting = part.status === "approval" && live && part.approval_id;

  const decide = async (decision: "allow" | "deny", scope: "once" | "chat" | "always" = "once") => {
    if (!part.approval_id) return;
    setBusy(true);
    setErr(null);
    try {
      await api.approve(part.approval_id, decision, scope);
    } catch (e: any) {
      setErr(e.message);
      setBusy(false);
    }
  };

  return (
    <div className={`tool-card status-${part.status}${awaiting ? " awaiting" : ""}`}>
      <button className="tool-head" onClick={() => setOpen(!open)}>
        <span className="tool-icon">
          <SourceIcon part={part} />
        </span>
        <span className="tool-label">
          {server && <span className="tool-server">{server}</span>}
          <span className="tool-name">{label}</span>
          {summary(part) && <span className="tool-arg">{summary(part)}</span>}
        </span>
        <StatusIcon status={part.status} />
        <ChevronRight size={15} className={`chev${open ? " open" : ""}`} />
      </button>

      {awaiting && (
        <div className="approval">
          <div className="approval-q">
            <ShieldCheck size={16} /> Darf ich das ausführen?
          </div>
          <pre className="approval-args">{JSON.stringify(part.args, null, 2)}</pre>
          <div className="approval-actions">
            <button className="btn primary" disabled={busy} onClick={() => decide("allow")} autoFocus>
              Erlauben
            </button>
            <button className="btn" disabled={busy} onClick={() => decide("allow", "chat")}>
              In diesem Chat immer
            </button>
            <button className="btn" disabled={busy} onClick={() => decide("allow", "always")}>
              Immer erlauben
            </button>
            <span className="grow" />
            <button className="btn danger-ghost" disabled={busy} onClick={() => decide("deny")}>
              Ablehnen
            </button>
          </div>
          {err && <div className="form-error">{err}</div>}
        </div>
      )}
      {part.status === "approval" && !live && <div className="tool-note">Freigabe wurde nicht erteilt.</div>}

      {open && (
        <div className="tool-body">
          <div className="tool-section-title">Eingabe</div>
          <pre>{JSON.stringify(part.args, null, 2)}</pre>
          {part.result !== undefined && (
            <>
              <div className="tool-section-title">Ergebnis</div>
              <pre>{part.result}</pre>
            </>
          )}
        </div>
      )}
    </div>
  );
}

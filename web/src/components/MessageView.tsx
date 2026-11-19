import { AlertTriangle, Check, ChevronRight, Copy, FileText, Lightbulb, Pencil, RotateCcw } from "lucide-react";
import { useEffect, useState } from "react";
import type { Message, Part, ToolPart } from "../types";
import { Markdown } from "./Markdown";
import { ToolCard } from "./ToolCard";

function Reasoning({ text, live }: { text: string; live: boolean }) {
  const [open, setOpen] = useState(live);
  useEffect(() => {
    if (!live) setOpen(false);
  }, [live]);
  return (
    <div className={`reasoning${open ? " open" : ""}`}>
      <button className="reasoning-head" onClick={() => setOpen(!open)}>
        <Lightbulb size={14} />
        <span className={live ? "shimmer" : ""}>{live ? "Denkt nach …" : "Denkprozess"}</span>
        <ChevronRight size={14} className={`chev${open ? " open" : ""}`} />
      </button>
      {open && (
        <div className="reasoning-body">
          <Markdown text={text} />
        </div>
      )}
    </div>
  );
}

function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      className="icon-btn small"
      title="Kopieren"
      onClick={() => {
        navigator.clipboard?.writeText(text);
        setDone(true);
        setTimeout(() => setDone(false), 1500);
      }}
    >
      {done ? <Check size={14} /> : <Copy size={14} />}
    </button>
  );
}

export function UserMessage({
  message,
  onEdit,
  disabled,
}: {
  message: Message;
  onEdit: (text: string) => void;
  disabled: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(message.content);
  const files = message.parts.filter((p) => p.type === "file") as Extract<Part, { type: "file" }>[];
  const images = message.parts.filter((p) => p.type === "image") as Extract<Part, { type: "image" }>[];

  if (editing) {
    return (
      <div className="msg user editing">
        <textarea
          className="edit-area"
          value={draft}
          autoFocus
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              if (draft.trim()) {
                setEditing(false);
                onEdit(draft);
              }
            }
            if (e.key === "Escape") setEditing(false);
          }}
        />
        <div className="edit-actions">
          <button className="btn" onClick={() => setEditing(false)}>
            Abbrechen
          </button>
          <button
            className="btn primary"
            disabled={!draft.trim()}
            onClick={() => {
              setEditing(false);
              onEdit(draft);
            }}
          >
            Senden
          </button>
        </div>
      </div>
    );
  }
  return (
    <div className="msg user">
      <div className="bubble">
        {(images.length > 0 || files.length > 0) && (
          <div className="attachments">
            {images.map((img, i) => (
              <img key={i} src={img.url} alt={img.name} className="thumb" />
            ))}
            {files.map((f, i) => (
              <span key={i} className="file-chip">
                <FileText size={13} /> {f.name}
              </span>
            ))}
          </div>
        )}
        {message.content && <div className="user-text">{message.content}</div>}
      </div>
      <div className="msg-actions">
        <CopyButton text={message.content} />
        {!disabled && (
          <button className="icon-btn small" title="Bearbeiten" onClick={() => setEditing(true)}>
            <Pencil size={14} />
          </button>
        )}
      </div>
    </div>
  );
}

export function AssistantMessage({
  parts,
  live,
  status,
  model,
  onRegenerate,
}: {
  parts: Part[];
  live: boolean;
  status?: string;
  model?: string | null;
  onRegenerate?: () => void;
}) {
  const text = parts
    .filter((p) => p.type === "text")
    .map((p) => (p as { text: string }).text)
    .join("");
  const lastIdx = parts.length - 1;
  const waitingFirst = live && parts.length === 0;
  return (
    <div className="msg assistant">
      {waitingFirst && (
        <div className="typing">
          <span />
          <span />
          <span />
        </div>
      )}
      {parts.map((p, i) => {
        switch (p.type) {
          case "reasoning":
            return <Reasoning key={i} text={p.text} live={live && i === lastIdx} />;
          case "text":
            return (
              <div key={i} className={live && i === lastIdx ? "streaming" : ""}>
                <Markdown text={p.text} />
              </div>
            );
          case "tool":
            return <ToolCard key={(p as ToolPart).call_id + i} part={p as ToolPart} live={live} />;
          case "notice":
            return (
              <div key={i} className="notice">
                {p.text}
              </div>
            );
          case "error":
            return (
              <div key={i} className="error-box">
                <AlertTriangle size={15} /> {p.text}
              </div>
            );
          default:
            return null;
        }
      })}
      {!live && (
        <div className="msg-actions">
          {text && <CopyButton text={text} />}
          {onRegenerate && (
            <button className="icon-btn small" title="Neu generieren" onClick={onRegenerate}>
              <RotateCcw size={14} />
            </button>
          )}
          {status === "cancelled" && <span className="badge">Abgebrochen</span>}
          {model && <span className="model-tag">{model}</span>}
        </div>
      )}
    </div>
  );
}

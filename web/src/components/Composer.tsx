import { ArrowUp, FileText, Image as ImageIcon, Lightbulb, Loader2, Paperclip, Square, X } from "lucide-react";
import { uploadFile } from "../api";
import { useEffect, useRef, useState } from "react";
import type { AppConfig, Attachment, ChatUsage, PermissionMode } from "../types";
import { ContextMeter } from "./ContextMeter";
import { Dropdown, ModePicker } from "./Pickers";

const KIND_LABEL: Record<string, string> = {
  pdf: "PDF",
  docx: "Word",
  pptx: "PowerPoint",
  xlsx: "Excel",
  image: "Bild",
  text: "Text",
  html: "HTML",
};

function chipInfo(a: Attachment): string {
  if (a.uploading) return "liest …";
  if (a.status === "error") return a.error || "nicht lesbar";
  const kind = KIND_LABEL[a.fileKind ?? ""] ?? a.fileKind ?? "";
  const unit = a.fileKind === "pptx" ? "Folien" : a.fileKind === "xlsx" ? "Blätter" : "S.";
  return [kind, a.pages ? `${a.pages} ${unit}` : ""].filter(Boolean).join(" · ");
}

export interface ComposerSettings {
  model: string | null;
  mode: PermissionMode;
  effort: string;
}

export function Composer({
  config,
  settings,
  onSettings,
  onSend,
  onStop,
  running,
  disabled,
  autoFocus,
  usage,
}: {
  config: AppConfig;
  settings: ComposerSettings;
  onSettings: (s: Partial<ComposerSettings>) => void;
  onSend: (text: string, attachments: Attachment[]) => void;
  onStop: () => void;
  running: boolean;
  disabled?: boolean;
  autoFocus?: boolean;
  usage?: ChatUsage | null;
}) {
  const [text, setText] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [drag, setDrag] = useState(false);
  const ta = useRef<HTMLTextAreaElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const el = ta.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 320) + "px";
  }, [text]);

  useEffect(() => {
    if (autoFocus) ta.current?.focus();
  }, [autoFocus]);

  const addFiles = async (files: FileList | File[]) => {
    setError(null);
    for (const f of Array.from(files)) {
      const localId = Math.random().toString(36).slice(2);
      const name = f.name || (f.type.startsWith("image/") ? "Bild.png" : "datei");
      setAttachments((prev) => [...prev, { name, kind: "file", localId, uploading: true }]);
      uploadFile(f)
        .then((info) =>
          setAttachments((prev) =>
            prev.map((a) =>
              a.localId === localId
                ? { ...a, uploading: false, file_id: info.id, fileKind: info.kind, pages: info.pages, status: info.status, error: info.error }
                : a,
            ),
          ),
        )
        .catch((e) => {
          setAttachments((prev) => prev.filter((a) => a.localId !== localId));
          setError(e.message);
        });
    }
  };
  const uploading = attachments.some((a) => a.uploading);

  const submit = () => {
    if (running || disabled || uploading) return;
    const ready = attachments.filter((a) => a.status !== "error");
    if (!text.trim() && !ready.length) return;
    onSend(text.trim(), ready);
    setText("");
    setAttachments([]);
    setError(null);
  };

  return (
    <div
      className={`composer${drag ? " drag" : ""}`}
      onDragOver={(e) => {
        e.preventDefault();
        setDrag(true);
      }}
      onDragLeave={() => setDrag(false)}
      onDrop={(e) => {
        e.preventDefault();
        setDrag(false);
        if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
      }}
    >
      {attachments.length > 0 && (
        <div className="composer-attachments">
          {attachments.map((a, i) => (
            <span key={a.localId ?? i} className={`file-chip removable${a.status === "error" ? " error" : ""}`} title={a.error ?? undefined}>
              {a.uploading ? <Loader2 size={13} className="spin" /> : a.fileKind === "image" ? <ImageIcon size={13} /> : <FileText size={13} />}
              <span className="chip-name">{a.name}</span>
              <span className="chip-info">{chipInfo(a)}</span>
              <button onClick={() => setAttachments(attachments.filter((_, j) => j !== i))} title="Entfernen">
                <X size={12} />
              </button>
            </span>
          ))}
        </div>
      )}
      <textarea
        ref={ta}
        rows={1}
        value={text}
        placeholder={settings.mode === "plan" ? "Was sollen wir planen?" : "Nachricht an dein Second Brain …"}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            submit();
          }
        }}
        onPaste={(e) => {
          const files = Array.from(e.clipboardData.files);
          if (files.length) {
            e.preventDefault();
            addFiles(files);
          }
        }}
      />
      <div className="composer-bar">
        <button className="icon-btn" title="Datei anhängen" onClick={() => fileInput.current?.click()}>
          <Paperclip size={17} />
        </button>
        <input
          ref={fileInput}
          type="file"
          multiple
          hidden
          onChange={(e) => {
            if (e.target.files) addFiles(Array.from(e.target.files));
            e.target.value = "";
          }}
        />
        <ModePicker value={settings.mode} onChange={(mode) => onSettings({ mode })} />
        <span className="grow" />
        <ContextMeter usage={usage} />
        {config.models.length > 1 && (
          <Dropdown
            value={settings.model || config.default_model || ""}
            options={config.models.map((m) => ({ value: m, label: m }))}
            onChange={(model) => onSettings({ model })}
            title="Modell"
          />
        )}
        <Dropdown
          value={settings.effort}
          icon={<Lightbulb size={14} />}
          options={[
            { value: "", label: "Standard" },
            ...config.reasoning_efforts.map((e) => ({ value: e, label: EFFORT_LABELS[e] ?? e })),
          ]}
          onChange={(effort) => onSettings({ effort })}
          title="Denkaufwand (für Reasoning-Modelle)"
        />
        {running ? (
          <button className="send-btn stop" onClick={onStop} title="Stoppen">
            <Square size={14} fill="currentColor" />
          </button>
        ) : (
          <button
            className="send-btn"
            onClick={submit}
            disabled={disabled || uploading || (!text.trim() && !attachments.length)}
            title="Senden (Enter)"
          >
            <ArrowUp size={18} />
          </button>
        )}
      </div>
      {error && <div className="form-error composer-error">{error}</div>}
    </div>
  );
}

const EFFORT_LABELS: Record<string, string> = { minimal: "Minimal", low: "Wenig", medium: "Mittel", high: "Gründlich" };

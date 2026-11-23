import { ArrowUp, FileText, Image as ImageIcon, Lightbulb, Paperclip, Square, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { AppConfig, Attachment, ChatUsage, PermissionMode } from "../types";
import { ContextMeter } from "./ContextMeter";
import { Dropdown, ModePicker } from "./Pickers";

const TEXT_EXT =
  /\.(txt|md|markdown|csv|tsv|json|jsonl|ya?ml|toml|ini|xml|html?|css|scss|js|jsx|ts|tsx|py|rb|go|rs|java|kt|swift|c|h|cpp|hpp|cs|php|sh|zsh|bash|sql|log|env|conf|tex|rst|org)$/i;
const MAX_TEXT = 300_000;

async function readFile(file: File): Promise<Attachment> {
  if (file.type.startsWith("image/")) {
    if (file.size > 10 * 1024 * 1024) throw new Error(`${file.name}: Bild ist größer als 10 MB`);
    const url = await new Promise<string>((resolve, reject) => {
      const r = new FileReader();
      r.onload = () => resolve(r.result as string);
      r.onerror = () => reject(r.error);
      r.readAsDataURL(file);
    });
    return { name: file.name || "Bild.png", kind: "image", url };
  }
  if (file.type.startsWith("text/") || TEXT_EXT.test(file.name) || file.type === "application/json") {
    const text = await file.text();
    return { name: file.name, kind: "text", text: text.slice(0, MAX_TEXT) };
  }
  throw new Error(`${file.name}: Dieser Dateityp wird noch nicht unterstützt (Text-Dateien und Bilder gehen).`);
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
      try {
        const a = await readFile(f);
        setAttachments((prev) => [...prev, a]);
      } catch (e: any) {
        setError(e.message);
      }
    }
  };

  const submit = () => {
    if (running || disabled) return;
    if (!text.trim() && !attachments.length) return;
    onSend(text.trim(), attachments);
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
            <span key={i} className="file-chip removable">
              {a.kind === "image" ? <ImageIcon size={13} /> : <FileText size={13} />}
              {a.name}
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
            if (e.target.files) addFiles(e.target.files);
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
            disabled={disabled || (!text.trim() && !attachments.length)}
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

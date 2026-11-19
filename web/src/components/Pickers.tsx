import { Check, ChevronDown, Map, ShieldQuestion, Zap } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import type { PermissionMode } from "../types";

export const MODES: { value: PermissionMode; label: string; icon: ReactNode; desc: string }[] = [
  { value: "plan", label: "Planen", icon: <Map size={15} />, desc: "Nur lesen & planen – nichts wird verändert" },
  { value: "ask", label: "Fragen", icon: <ShieldQuestion size={15} />, desc: "Lesen sofort, Änderungen nach deiner Freigabe" },
  { value: "auto", label: "Auto", icon: <Zap size={15} />, desc: "Alles läuft ohne Rückfrage" },
];

function usePopover() {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  return { open, setOpen, ref };
}

export function ModePicker({ value, onChange }: { value: PermissionMode; onChange: (m: PermissionMode) => void }) {
  const { open, setOpen, ref } = usePopover();
  const current = MODES.find((m) => m.value === value) ?? MODES[1];
  return (
    <div className="popover-wrap" ref={ref}>
      <button className={`pill mode-${current.value}`} onClick={() => setOpen(!open)} title={current.desc}>
        {current.icon}
        <span>{current.label}</span>
        <ChevronDown size={13} />
      </button>
      {open && (
        <div className="popover up">
          <div className="popover-title">Freigabe-Modus</div>
          {MODES.map((m) => (
            <button
              key={m.value}
              className={`popover-item${m.value === value ? " active" : ""}`}
              onClick={() => {
                onChange(m.value);
                setOpen(false);
              }}
            >
              <span className={`mode-icon mode-${m.value}`}>{m.icon}</span>
              <span className="popover-text">
                <span className="popover-label">{m.label}</span>
                <span className="popover-desc">{m.desc}</span>
              </span>
              {m.value === value && <Check size={15} />}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function Dropdown({
  value,
  options,
  onChange,
  title,
  icon,
}: {
  value: string;
  options: { value: string; label: string }[];
  onChange: (v: string) => void;
  title?: string;
  icon?: ReactNode;
}) {
  const { open, setOpen, ref } = usePopover();
  const current = options.find((o) => o.value === value) ?? options[0];
  return (
    <div className="popover-wrap" ref={ref}>
      <button className="pill subtle" onClick={() => setOpen(!open)} title={title}>
        {icon}
        <span>{current?.label}</span>
        <ChevronDown size={13} />
      </button>
      {open && (
        <div className="popover up right">
          {title && <div className="popover-title">{title}</div>}
          {options.map((o) => (
            <button
              key={o.value}
              className={`popover-item${o.value === value ? " active" : ""}`}
              onClick={() => {
                onChange(o.value);
                setOpen(false);
              }}
            >
              <span className="popover-text">
                <span className="popover-label">{o.label}</span>
              </span>
              {o.value === value && <Check size={15} />}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

import { useEffect, useRef, useState } from "react";
import { fmtCost, fmtTokens } from "../format";
import type { ChatUsage } from "../types";

/** Kleiner Ring im Eingabefeld: wie voll ist das Kontextfenster, was hat der Chat gekostet? */
export function ContextMeter({ usage }: { usage: ChatUsage | null | undefined }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && setOpen(false);
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);

  if (!usage || !usage.total_tokens) return null;
  const win = usage.context_window;
  const ratio = win ? Math.min(1, usage.context_tokens / win) : null;
  const level = ratio === null ? "none" : ratio >= 0.85 ? "high" : ratio >= 0.6 ? "mid" : "low";
  const r = 7;
  const circ = 2 * Math.PI * r;

  return (
    <div className="popover-wrap" ref={ref}>
      <button
        className={`context-meter level-${level}`}
        onClick={() => setOpen(!open)}
        title={ratio !== null ? `Kontext ${Math.round(ratio * 100)} % belegt` : `${fmtTokens(usage.total_tokens)} Tokens`}
      >
        {ratio !== null ? (
          <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
            <circle cx="9" cy="9" r={r} className="ring-bg" />
            <circle
              cx="9"
              cy="9"
              r={r}
              className="ring-fg"
              strokeDasharray={`${circ * ratio} ${circ}`}
              transform="rotate(-90 9 9)"
            />
          </svg>
        ) : null}
        <span className="context-label">
          {ratio !== null ? `${Math.round(ratio * 100)} %` : `${fmtTokens(usage.total_tokens)} Tokens`}
        </span>
      </button>
      {open && (
        <div className="popover up right usage-pop">
          <div className="popover-title">Dieser Chat</div>
          <div className="usage-grid">
            <span>Kontext</span>
            <b>
              {fmtTokens(usage.context_tokens)}
              {win ? ` / ${fmtTokens(win)}` : ""}
            </b>
            <span>Verbraucht gesamt</span>
            <b>{usage.total_tokens.toLocaleString("de-DE")} Tokens</b>
            <span className="sub">davon Eingabe</span>
            <span className="sub">{usage.prompt_tokens.toLocaleString("de-DE")}</span>
            <span className="sub">davon Ausgabe</span>
            <span className="sub">{usage.completion_tokens.toLocaleString("de-DE")}</span>
            <span>Kosten</span>
            <b>
              {fmtCost(usage.cost)}
              {usage.partially_priced ? " (teilweise)" : ""}
            </b>
          </div>
          {!win && <div className="usage-hint">Kontextfenster unbekannt – in den Einstellungen unter „Modelle & Kosten“ eintragen.</div>}
          {usage.cost === null && <div className="usage-hint">Keine Preise für {usage.model ?? "dieses Modell"} bekannt.</div>}
          {ratio !== null && ratio >= 0.85 && (
            <div className="usage-hint warn">Der Kontext ist fast voll – für neue Themen lieber einen neuen Chat starten.</div>
          )}
        </div>
      )}
    </div>
  );
}

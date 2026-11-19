import type { Part, RunEvent, ToolPart } from "./types";

/** Spiegel von PartsBuilder im Backend: baut aus Run-Events die Teile einer Antwort. */
export function applyEvent(parts: Part[], ev: RunEvent): Part[] {
  const out = parts.slice();
  const last = out[out.length - 1];
  const findTool = (id: string) => {
    for (let i = out.length - 1; i >= 0; i--) {
      const p = out[i];
      if (p.type === "tool" && p.call_id === id) return i;
    }
    return -1;
  };
  const updateTool = (id: string, patch: Partial<ToolPart>) => {
    const i = findTool(id);
    if (i >= 0) out[i] = { ...(out[i] as ToolPart), ...patch };
  };
  switch (ev.type) {
    case "text":
    case "reasoning":
      if (last && last.type === ev.type) out[out.length - 1] = { ...last, text: last.text + ev.delta };
      else out.push({ type: ev.type, text: ev.type === "reasoning" ? ev.delta.replace(/^\n+/, "") : ev.delta });
      break;
    case "tool_start":
      out.push({
        type: "tool",
        call_id: ev.call_id,
        name: ev.name,
        title: ev.title || ev.name,
        source: ev.source || "",
        kind: ev.kind || "",
        args: ev.args || {},
        status: "running",
      });
      break;
    case "approval_required":
      updateTool(ev.call_id, { status: "approval", approval_id: ev.approval_id });
      break;
    case "approval_resolved":
      updateTool(ev.call_id, {
        status: ev.decision === "allow" ? "running" : "denied",
        approved: ev.decision === "allow",
      });
      break;
    case "tool_end": {
      const i = findTool(ev.call_id);
      if (i >= 0) {
        const p = out[i] as ToolPart;
        out[i] = { ...p, status: p.status === "denied" ? "denied" : ev.is_error ? "error" : "done", result: ev.result };
      }
      break;
    }
    case "activity":
      for (const line of ev.lines || []) out.push({ type: "notice", text: line });
      break;
    case "error":
      out.push({ type: "error", text: ev.message || "Fehler" });
      break;
  }
  return out;
}

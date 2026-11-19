import type { AppConfig, Attachment, ChatDetail, ChatSummary, RunEvent } from "./types";

const KEY = "sb.apiKey";

export class AuthError extends Error {}

export function getApiKey(): string {
  try {
    return localStorage.getItem(KEY) || "";
  } catch {
    return "";
  }
}

export function setApiKey(key: string) {
  try {
    localStorage.setItem(KEY, key);
  } catch {
    /* ignore */
  }
}

function headers(json = true): HeadersInit {
  const h: Record<string, string> = {};
  if (json) h["Content-Type"] = "application/json";
  const key = getApiKey();
  if (key) h["Authorization"] = `Bearer ${key}`;
  return h;
}

export async function request<T = any>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: headers(body !== undefined),
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) throw new AuthError("API-Key fehlt oder ist falsch");
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const data = await res.json();
      detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail ?? data);
    } catch {
      /* ignore */
    }
    throw new Error(detail || `HTTP ${res.status}`);
  }
  return (res.status === 204 ? undefined : await res.json()) as T;
}

export const api = {
  config: () => request<AppConfig>("GET", "/api/config"),
  chats: (q?: string) => request<ChatSummary[]>("GET", `/api/chats${q ? `?q=${encodeURIComponent(q)}` : ""}`),
  createChat: (body: { incognito?: boolean; model?: string | null; permission_mode?: string | null }) =>
    request<ChatSummary>("POST", "/api/chats", body),
  chat: (id: string) => request<ChatDetail>("GET", `/api/chats/${id}`),
  patchChat: (id: string, body: Record<string, unknown>) => request("PATCH", `/api/chats/${id}`, body),
  deleteChat: (id: string) => request("DELETE", `/api/chats/${id}`),
  send: (
    id: string,
    body: {
      content: string;
      attachments?: Attachment[];
      model?: string | null;
      permission_mode?: string | null;
      reasoning_effort?: string | null;
      edit_message_id?: number;
    },
  ) => request<{ run_id: string; user_message_id: number }>("POST", `/api/chats/${id}/messages`, body),
  regenerate: (id: string, body: { model?: string | null; reasoning_effort?: string | null }) =>
    request<{ run_id: string }>("POST", `/api/chats/${id}/regenerate`, body),
  cancel: (runId: string) => request("POST", `/api/runs/${runId}/cancel`),
  approve: (approvalId: string, decision: "allow" | "deny", scope: "once" | "chat" | "always" = "once") =>
    request("POST", `/api/approvals/${approvalId}`, { decision, scope }),
};

/** Liest den Event-Stream eines Runs (SSE über fetch, damit der API-Key mitgeht). */
export async function* runEvents(runId: string, after: number, signal: AbortSignal): AsyncGenerator<RunEvent> {
  const res = await fetch(`/api/runs/${runId}/events?after=${after}`, { headers: headers(false), signal });
  if (res.status === 401) throw new AuthError("API-Key fehlt oder ist falsch");
  if (!res.ok || !res.body) throw new Error(`Event-Stream: HTTP ${res.status}`);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      for (const line of block.split("\n")) {
        if (line.startsWith("data: ")) yield JSON.parse(line.slice(6));
      }
    }
  }
}

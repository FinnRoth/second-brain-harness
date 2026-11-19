export type PermissionMode = "plan" | "ask" | "auto";

export interface AppConfig {
  user_name: string;
  models: string[];
  default_model: string | null;
  permission_modes: PermissionMode[];
  default_permission_mode: PermissionMode;
  reasoning_efforts: string[];
}

export interface ChatSummary {
  id: string;
  title: string | null;
  model: string | null;
  permission_mode: PermissionMode | null;
  pinned: boolean;
  incognito: boolean;
  running?: boolean;
  created_at: string;
  updated_at: string;
}

export type Part =
  | { type: "text"; text: string }
  | { type: "reasoning"; text: string }
  | { type: "notice"; text: string }
  | { type: "error"; text: string }
  | { type: "file"; name: string; text?: string; chars?: number }
  | { type: "image"; name: string; url: string }
  | ToolPart;

export interface ToolPart {
  type: "tool";
  call_id: string;
  name: string;
  title: string;
  source: string;
  kind: string;
  args: Record<string, unknown>;
  status: "running" | "approval" | "done" | "error" | "denied";
  approval_id?: string;
  approved?: boolean;
  result?: string;
}

export interface Message {
  id: number;
  role: "user" | "assistant";
  content: string;
  parts: Part[];
  model?: string | null;
  status: string;
  created_at: string;
}

export interface ChatDetail extends ChatSummary {
  messages: Message[];
  active_run: { id: string; model: string } | null;
  allowed_tools: string[];
}

export interface RunEvent {
  seq: number;
  type: string;
  [key: string]: any;
}

export interface Attachment {
  name: string;
  kind: "text" | "image";
  text?: string;
  url?: string;
}

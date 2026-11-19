import { ArrowDown, Ghost, MoreHorizontal, Pin, PinOff, Trash2 } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { AuthError, api, runEvents } from "../api";
import { Composer, type ComposerSettings } from "../components/Composer";
import { AssistantMessage, UserMessage } from "../components/MessageView";
import { applyEvent } from "../parts";
import type { AppConfig, Attachment, ChatDetail, Message, Part, PermissionMode } from "../types";

interface Live {
  runId: string;
  parts: Part[];
  model: string | null;
}

const LS_EFFORT = "sb.effort";
const LS_MODE = "sb.mode";
const LS_MODEL = "sb.model";

function ls(key: string, fallback = ""): string {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}
function lsSet(key: string, value: string) {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* ignore */
  }
}

function greeting(name: string) {
  const h = new Date().getHours();
  const g = h < 5 ? "Gute Nacht" : h < 11 ? "Guten Morgen" : h < 18 ? "Hallo" : "Guten Abend";
  return name && name !== "der Nutzer" ? `${g}, ${name}` : g;
}

const SUGGESTIONS = [
  "Was steht gerade bei meinen Projekten an?",
  "Fass zusammen, was du über mich weißt",
  "Lass uns eine Strategie für ein Vorhaben entwickeln",
  "Wen habe ich in letzter Zeit kennengelernt?",
];

export function ChatView({
  config,
  chatId,
  onNavigate,
  onChatsChanged,
  onAuthError,
}: {
  config: AppConfig;
  chatId: string | null;
  onNavigate: (id: string | null) => void;
  onChatsChanged: () => void;
  onAuthError: () => void;
}) {
  const [chat, setChat] = useState<ChatDetail | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [live, setLive] = useState<Live | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [incognito, setIncognito] = useState(false);
  const [menu, setMenu] = useState(false);
  const [editingTitle, setEditingTitle] = useState(false);
  const [settings, setSettings] = useState<ComposerSettings>(() => ({
    model: ls(LS_MODEL) || config.default_model,
    mode: (ls(LS_MODE) as PermissionMode) || config.default_permission_mode,
    effort: ls(LS_EFFORT),
  }));
  const scroller = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const [showDown, setShowDown] = useState(false);
  const follower = useRef<AbortController | null>(null);
  const pendingNav = useRef<string | null>(null);

  const handleError = useCallback(
    (e: any) => {
      if (e instanceof AuthError) onAuthError();
      else setError(e?.message || String(e));
    },
    [onAuthError],
  );

  // ----------------------------------------------------------- Run verfolgen
  const follow = useCallback(
    async (runId: string, model: string | null, id: string) => {
      follower.current?.abort();
      const ctrl = new AbortController();
      follower.current = ctrl;
      let parts: Part[] = [];
      let lastSeq = -1;
      let frame = 0;
      const flush = () => {
        frame = 0;
        setLive({ runId, parts, model });
      };
      setLive({ runId, parts, model });
      let attempts = 0;
      while (!ctrl.signal.aborted) {
        try {
          for await (const ev of runEvents(runId, lastSeq, ctrl.signal)) {
            lastSeq = ev.seq;
            attempts = 0;
            if (ev.type === "title") onChatsChanged();
            if (ev.type === "end") break;
            const next = applyEvent(parts, ev);
            if (next !== parts) {
              parts = next;
              if (!frame) frame = requestAnimationFrame(flush);
            }
          }
          break;
        } catch (e: any) {
          if (ctrl.signal.aborted) return;
          if (e instanceof AuthError) return onAuthError();
          if (String(e?.message).includes("404")) break; // Run ist schon vorbei
          if (++attempts > 5) {
            setError("Verbindung zum Harness verloren.");
            break;
          }
          await new Promise((r) => setTimeout(r, 800 * attempts));
        }
      }
      if (frame) cancelAnimationFrame(frame);
      if (ctrl.signal.aborted) return;
      try {
        const fresh = await api.chat(id);
        setChat(fresh);
        setMessages(fresh.messages);
      } catch (e) {
        handleError(e);
      }
      setLive(null);
      onChatsChanged();
    },
    [handleError, onAuthError, onChatsChanged],
  );

  // ---------------------------------------------------------- Chat laden
  useEffect(() => {
    follower.current?.abort();
    setError(null);
    setLive(null);
    setMenu(false);
    if (!chatId) {
      setChat(null);
      setMessages([]);
      return;
    }
    if (pendingNav.current === chatId) {
      pendingNav.current = null; // gerade selbst angelegt – Zustand ist schon da
      return;
    }
    let cancelled = false;
    api
      .chat(chatId)
      .then((c) => {
        if (cancelled) return;
        setChat(c);
        setMessages(c.messages);
        setSettings((s) => ({
          ...s,
          model: c.model || s.model,
          mode: (c.permission_mode as PermissionMode) || s.mode,
        }));
        stick.current = true;
        if (c.active_run) follow(c.active_run.id, c.active_run.model, c.id);
      })
      .catch((e) => {
        if (!cancelled) handleError(e);
      });
    return () => {
      cancelled = true;
    };
  }, [chatId, follow, handleError]);

  useEffect(() => () => follower.current?.abort(), []);

  // ----------------------------------------------------------- Scrollen
  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [messages, live]);

  const onScroll = () => {
    const el = scroller.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    stick.current = atBottom;
    setShowDown(!atBottom);
  };
  const scrollDown = () => {
    const el = scroller.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    stick.current = true;
  };

  // ------------------------------------------------------------ Aktionen
  const updateSettings = (patch: Partial<ComposerSettings>) => {
    setSettings((s) => ({ ...s, ...patch }));
    if (patch.effort !== undefined) lsSet(LS_EFFORT, patch.effort);
    if (patch.mode) lsSet(LS_MODE, patch.mode);
    if (patch.model) lsSet(LS_MODEL, patch.model);
    if (chat && (patch.mode || patch.model)) {
      api
        .patchChat(chat.id, { permission_mode: patch.mode, model: patch.model })
        .then(onChatsChanged)
        .catch(handleError);
    }
  };

  const send = async (text: string, attachments: Attachment[], editId?: number) => {
    setError(null);
    try {
      let id = chat?.id;
      let current = chat;
      if (!id) {
        const created = await api.createChat({
          incognito,
          model: settings.model,
          permission_mode: settings.mode,
        });
        id = created.id;
        current = { ...(created as any), messages: [], active_run: null, allowed_tools: [] };
        setChat(current);
        pendingNav.current = id;
        onNavigate(id);
      }
      const optimistic: Message = {
        id: -Date.now(),
        role: "user",
        content: text,
        parts: [
          ...(text ? [{ type: "text" as const, text }] : []),
          ...attachments.map((a) =>
            a.kind === "image"
              ? { type: "image" as const, name: a.name, url: a.url! }
              : { type: "file" as const, name: a.name },
          ),
        ],
        status: "done",
        created_at: new Date().toISOString(),
      };
      setMessages((prev) => {
        const base = editId !== undefined ? prev.slice(0, prev.findIndex((m) => m.id === editId)) : prev;
        return [...base, optimistic];
      });
      stick.current = true;
      const { run_id } = await api.send(id!, {
        content: text,
        attachments,
        model: settings.model,
        permission_mode: settings.mode,
        reasoning_effort: settings.effort || null,
        edit_message_id: editId,
      });
      follow(run_id, settings.model, id!);
      onChatsChanged();
    } catch (e) {
      handleError(e);
    }
  };

  const regenerate = async () => {
    if (!chat) return;
    setError(null);
    try {
      const lastUser = messages.map((m) => m.role).lastIndexOf("user");
      setMessages(messages.slice(0, lastUser + 1));
      const { run_id } = await api.regenerate(chat.id, {
        model: settings.model,
        reasoning_effort: settings.effort || null,
      });
      follow(run_id, settings.model, chat.id);
    } catch (e) {
      handleError(e);
    }
  };

  const stop = async () => {
    if (live) await api.cancel(live.runId).catch(() => undefined);
  };

  const rename = async (title: string) => {
    setEditingTitle(false);
    if (!chat || !title.trim() || title === chat.title) return;
    setChat({ ...chat, title });
    await api.patchChat(chat.id, { title }).catch(handleError);
    onChatsChanged();
  };

  const togglePin = async () => {
    if (!chat) return;
    setMenu(false);
    await api.patchChat(chat.id, { pinned: !chat.pinned }).catch(handleError);
    setChat({ ...chat, pinned: !chat.pinned });
    onChatsChanged();
  };

  const remove = async () => {
    if (!chat) return;
    setMenu(false);
    if (!confirm("Diesen Chat löschen? Was daraus gelernt wurde, bleibt im Gedächtnis.")) return;
    await api.deleteChat(chat.id).catch(handleError);
    onChatsChanged();
    onNavigate(null);
  };

  // ------------------------------------------------------------- Rendern
  const running = !!live;
  const lastAssistant = messages.map((m) => m.role).lastIndexOf("assistant");
  const isIncognito = chat ? chat.incognito : incognito;
  const empty = messages.length === 0 && !live;

  return (
    <div className={`chat${isIncognito ? " incognito" : ""}`}>
      <header className="chat-header">
        {chat ? (
          editingTitle ? (
            <input
              className="title-input"
              defaultValue={chat.title || ""}
              autoFocus
              onBlur={(e) => rename(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") rename((e.target as HTMLInputElement).value);
                if (e.key === "Escape") setEditingTitle(false);
              }}
            />
          ) : (
            <button className="chat-title" onClick={() => setEditingTitle(true)} title="Umbenennen">
              {chat.title || "Neuer Chat"}
            </button>
          )
        ) : (
          <span className="chat-title muted">Neuer Chat</span>
        )}
        {isIncognito && (
          <span className="badge ghost">
            <Ghost size={13} /> Inkognito – wird nicht gespeichert
          </span>
        )}
        <span className="grow" />
        {!chat && (
          <button
            className={`pill${incognito ? " active-ghost" : " subtle"}`}
            onClick={() => setIncognito(!incognito)}
            title="Inkognito: Das Gedächtnis wird gelesen, aber nichts gespeichert"
          >
            <Ghost size={15} /> Inkognito
          </button>
        )}
        {chat && (
          <div className="popover-wrap">
            <button className="icon-btn" onClick={() => setMenu(!menu)} title="Mehr">
              <MoreHorizontal size={18} />
            </button>
            {menu && (
              <div className="popover right" onMouseLeave={() => setMenu(false)}>
                {!chat.incognito && (
                  <button className="popover-item" onClick={togglePin}>
                    {chat.pinned ? <PinOff size={15} /> : <Pin size={15} />}
                    <span className="popover-label">{chat.pinned ? "Lösen" : "Anheften"}</span>
                  </button>
                )}
                <button className="popover-item danger" onClick={remove}>
                  <Trash2 size={15} />
                  <span className="popover-label">Löschen</span>
                </button>
              </div>
            )}
          </div>
        )}
      </header>

      <div className="scroller" ref={scroller} onScroll={onScroll}>
        {empty ? (
          <div className="empty">
            <div className="empty-logo">{isIncognito ? <Ghost size={30} /> : "🧠"}</div>
            <h1>{isIncognito ? "Inkognito-Chat" : greeting(config.user_name)}</h1>
            <p className="muted">
              {isIncognito
                ? "Ich kenne dich, aber nichts aus diesem Chat wird gespeichert."
                : "Was beschäftigt dich gerade?"}
            </p>
            {!isIncognito && (
              <div className="suggestions">
                {SUGGESTIONS.map((s) => (
                  <button key={s} className="suggestion" onClick={() => send(s, [])}>
                    {s}
                  </button>
                ))}
              </div>
            )}
          </div>
        ) : (
          <div className="messages">
            {messages.map((m, i) =>
              m.role === "user" ? (
                <UserMessage
                  key={m.id}
                  message={m}
                  disabled={running || m.id < 0}
                  onEdit={(text) =>
                    send(
                      text,
                      m.parts
                        .filter((p) => p.type === "image" || (p.type === "file" && p.text))
                        .map((p: any) =>
                          p.type === "image"
                            ? { name: p.name, kind: "image" as const, url: p.url }
                            : { name: p.name, kind: "text" as const, text: p.text },
                        ),
                      m.id,
                    )
                  }
                />
              ) : (
                <AssistantMessage
                  key={m.id}
                  parts={m.parts}
                  live={false}
                  status={m.status}
                  model={m.model}
                  onRegenerate={!running && i === lastAssistant ? regenerate : undefined}
                />
              ),
            )}
            {live && <AssistantMessage parts={live.parts} live />}
          </div>
        )}
      </div>

      <div className="composer-wrap">
        {showDown && (
          <button className="scroll-down" onClick={scrollDown} title="Nach unten">
            <ArrowDown size={16} />
          </button>
        )}
        {error && (
          <div className="error-banner" onClick={() => setError(null)}>
            {error}
          </div>
        )}
        <Composer
          config={config}
          settings={settings}
          onSettings={updateSettings}
          onSend={(t, a) => send(t, a)}
          onStop={stop}
          running={running}
          autoFocus={!chatId}
        />
        <div className="composer-hint">
          Enter senden · Shift+Enter neue Zeile · Dateien per Drag & Drop
        </div>
      </div>
    </div>
  );
}

import { Brain, Ghost, Loader2, MoreHorizontal, PanelLeftClose, Pencil, Pin, PinOff, Plug, Search, Settings, Sparkles, SquarePen, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { api } from "../api";
import type { ChatSummary } from "../types";

function group(chats: ChatSummary[]): [string, ChatSummary[]][] {
  const now = new Date();
  const startOfDay = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const day = 86_400_000;
  const groups: Record<string, ChatSummary[]> = {};
  const order = ["Angeheftet", "Heute", "Gestern", "Letzte 7 Tage", "Letzte 30 Tage", "Älter"];
  for (const c of chats) {
    const t = new Date(c.updated_at).getTime();
    const key = c.pinned
      ? "Angeheftet"
      : t >= startOfDay
        ? "Heute"
        : t >= startOfDay - day
          ? "Gestern"
          : t >= startOfDay - 7 * day
            ? "Letzte 7 Tage"
            : t >= startOfDay - 30 * day
              ? "Letzte 30 Tage"
              : "Älter";
    (groups[key] ||= []).push(c);
  }
  return order.filter((k) => groups[k]?.length).map((k) => [k, groups[k]]);
}

function ChatItem({
  chat,
  active,
  onOpen,
  onChanged,
  onDeleted,
}: {
  chat: ChatSummary;
  active: boolean;
  onOpen: () => void;
  onChanged: () => void;
  onDeleted: () => void;
}) {
  const [menu, setMenu] = useState(false);
  const [renaming, setRenaming] = useState(false);

  const rename = async (title: string) => {
    setRenaming(false);
    if (title.trim() && title !== chat.title) {
      await api.patchChat(chat.id, { title });
      onChanged();
    }
  };

  if (renaming) {
    return (
      <input
        className="chat-item-input"
        defaultValue={chat.title || ""}
        autoFocus
        onBlur={(e) => rename(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") rename((e.target as HTMLInputElement).value);
          if (e.key === "Escape") setRenaming(false);
        }}
      />
    );
  }
  return (
    <div className={`chat-item${active ? " active" : ""}`} onMouseLeave={() => setMenu(false)}>
      <button className="chat-item-main" onClick={onOpen} title={chat.title || ""}>
        {chat.incognito && <Ghost size={14} className="muted" />}
        <span className="chat-item-title">{chat.title || "Neuer Chat"}</span>
        {chat.running && <Loader2 size={13} className="spin muted" />}
      </button>
      <button className="icon-btn small chat-item-more" onClick={() => setMenu(!menu)} title="Optionen">
        <MoreHorizontal size={15} />
      </button>
      {menu && (
        <div className="popover right below">
          <button className="popover-item" onClick={() => (setMenu(false), setRenaming(true))}>
            <Pencil size={14} /> <span className="popover-label">Umbenennen</span>
          </button>
          {!chat.incognito && (
            <button
              className="popover-item"
              onClick={async () => {
                setMenu(false);
                await api.patchChat(chat.id, { pinned: !chat.pinned });
                onChanged();
              }}
            >
              {chat.pinned ? <PinOff size={14} /> : <Pin size={14} />}
              <span className="popover-label">{chat.pinned ? "Lösen" : "Anheften"}</span>
            </button>
          )}
          <button
            className="popover-item danger"
            onClick={async () => {
              setMenu(false);
              if (!confirm(`„${chat.title || "Chat"}“ löschen?`)) return;
              await api.deleteChat(chat.id);
              onDeleted();
            }}
          >
            <Trash2 size={14} /> <span className="popover-label">Löschen</span>
          </button>
        </div>
      )}
    </div>
  );
}

export function Sidebar({
  chats,
  route,
  onNavigate,
  onNewChat,
  onChanged,
  onClose,
  search,
  onSearch,
}: {
  chats: ChatSummary[];
  route: string;
  onNavigate: (hash: string) => void;
  onNewChat: () => void;
  onChanged: () => void;
  onClose: () => void;
  search: string;
  onSearch: (q: string) => void;
}) {
  const groups = useMemo(() => group(chats), [chats]);
  const activeId = route.startsWith("#/chat/") ? route.slice(7) : null;

  return (
    <aside className="sidebar">
      <div className="sidebar-top">
        <div className="brand">
          <span className="brand-logo">🧠</span> Second Brain
        </div>
        <button className="icon-btn" onClick={onClose} title="Seitenleiste ausblenden">
          <PanelLeftClose size={18} />
        </button>
      </div>
      <button className="new-chat" onClick={onNewChat} title="Neuer Chat (⌘⇧O)">
        <SquarePen size={16} /> Neuer Chat
      </button>
      <div className="search">
        <Search size={15} />
        <input placeholder="Chats durchsuchen" value={search} onChange={(e) => onSearch(e.target.value)} />
      </div>
      <nav className="chat-list">
        {groups.map(([label, items]) => (
          <div key={label} className="chat-group">
            <div className="chat-group-label">{label}</div>
            {items.map((c) => (
              <ChatItem
                key={c.id}
                chat={c}
                active={c.id === activeId}
                onOpen={() => onNavigate(`#/chat/${c.id}`)}
                onChanged={onChanged}
                onDeleted={() => {
                  onChanged();
                  if (c.id === activeId) onNavigate("#/");
                }}
              />
            ))}
          </div>
        ))}
        {!chats.length && <div className="muted small pad">{search ? "Nichts gefunden." : "Noch keine Chats."}</div>}
      </nav>
      <div className="sidebar-bottom">
        <button className={`nav-item${route === "#/memory" ? " active" : ""}`} onClick={() => onNavigate("#/memory")}>
          <Brain size={17} /> Gedächtnis
        </button>
        <button className={`nav-item${route === "#/tools" ? " active" : ""}`} onClick={() => onNavigate("#/tools")}>
          <Plug size={17} /> Tools & MCP
        </button>
        <button className={`nav-item${route === "#/skills" ? " active" : ""}`} onClick={() => onNavigate("#/skills")}>
          <Sparkles size={17} /> Skills
        </button>
        <button
          className={`nav-item${route === "#/settings" ? " active" : ""}`}
          onClick={() => onNavigate("#/settings")}
        >
          <Settings size={17} /> Einstellungen
        </button>
      </div>
    </aside>
  );
}

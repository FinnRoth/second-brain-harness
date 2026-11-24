import { PanelLeftOpen, SquarePen } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { AuthError, api } from "./api";
import { KeyPrompt } from "./components/KeyPrompt";
import { Sidebar } from "./components/Sidebar";
import { ChatView } from "./pages/ChatView";
import { MemoryPage } from "./pages/MemoryPage";
import { SettingsPage } from "./pages/SettingsPage";
import { SkillsPage } from "./pages/SkillsPage";
import { ToolsPage } from "./pages/ToolsPage";
import type { AppConfig, ChatSummary } from "./types";

function useHash() {
  const [hash, setHash] = useState(() => window.location.hash || "#/");
  useEffect(() => {
    const on = () => setHash(window.location.hash || "#/");
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  const navigate = useCallback((h: string) => {
    if (window.location.hash !== h) window.location.hash = h;
    setHash(h);
  }, []);
  return [hash, navigate] as const;
}

export default function App() {
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [needsKey, setNeedsKey] = useState(false);
  const [fatal, setFatal] = useState<string | null>(null);
  const [chats, setChats] = useState<ChatSummary[]>([]);
  const [search, setSearch] = useState("");
  const [route, navigate] = useHash();
  const [sidebar, setSidebar] = useState(() => window.innerWidth > 800);
  const [newChatKey, setNewChatKey] = useState(0);

  const load = useCallback(() => {
    setFatal(null);
    api
      .config()
      .then((c) => {
        setConfig(c);
        setNeedsKey(false);
      })
      .catch((e) => (e instanceof AuthError ? setNeedsKey(true) : setFatal(e.message)));
  }, []);
  useEffect(load, [load]);

  const refreshChats = useCallback(() => {
    api
      .chats(search || undefined)
      .then(setChats)
      .catch((e) => e instanceof AuthError && setNeedsKey(true));
  }, [search]);

  useEffect(() => {
    if (!config) return;
    const t = setTimeout(refreshChats, search ? 200 : 0);
    return () => clearTimeout(t);
  }, [config, refreshChats, search]);

  const newChat = useCallback(() => {
    navigate("#/");
    setNewChatKey((k) => k + 1);
    if (window.innerWidth <= 800) setSidebar(false);
  }, [navigate]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key.toLowerCase() === "o") {
        e.preventDefault();
        newChat();
      }
      if ((e.metaKey || e.ctrlKey) && e.key === "\\") {
        e.preventDefault();
        setSidebar((s) => !s);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [newChat]);

  if (needsKey) return <KeyPrompt onDone={load} />;
  if (fatal)
    return (
      <div className="center-screen">
        <h2>Harness nicht erreichbar</h2>
        <p className="muted">{fatal}</p>
        <button className="btn primary" onClick={load}>
          Erneut versuchen
        </button>
      </div>
    );
  if (!config) return <div className="center-screen muted">Lädt …</div>;

  const chatId = route.startsWith("#/chat/") ? route.slice(7) : null;
  const go = (h: string) => {
    navigate(h);
    if (window.innerWidth <= 800) setSidebar(false);
  };

  return (
    <div className={`app${sidebar ? "" : " sidebar-hidden"}`}>
      {sidebar && (
        <>
          <Sidebar
            chats={chats}
            route={route}
            onNavigate={go}
            onNewChat={newChat}
            onChanged={refreshChats}
            onClose={() => setSidebar(false)}
            search={search}
            onSearch={setSearch}
          />
          <div className="sidebar-scrim" onClick={() => setSidebar(false)} />
        </>
      )}
      <main className="main">
        {!sidebar && (
          <div className="floating-tools">
            <button className="icon-btn" onClick={() => setSidebar(true)} title="Seitenleiste (⌘\)">
              <PanelLeftOpen size={18} />
            </button>
            <button className="icon-btn" onClick={newChat} title="Neuer Chat (⌘⇧O)">
              <SquarePen size={18} />
            </button>
          </div>
        )}
        {route === "#/memory" ? (
          <MemoryPage onAuthError={() => setNeedsKey(true)} />
        ) : route === "#/tools" ? (
          <ToolsPage onAuthError={() => setNeedsKey(true)} />
        ) : route === "#/skills" ? (
          <SkillsPage onAuthError={() => setNeedsKey(true)} />
        ) : route === "#/settings" ? (
          <SettingsPage onAuthError={() => setNeedsKey(true)} />
        ) : (
          <ChatView
            key={`view-${newChatKey}`}
            config={config}
            chatId={chatId}
            onNavigate={(id) => navigate(id ? `#/chat/${id}` : "#/")}
            onChatsChanged={refreshChats}
            onAuthError={() => setNeedsKey(true)}
          />
        )}
      </main>
    </div>
  );
}

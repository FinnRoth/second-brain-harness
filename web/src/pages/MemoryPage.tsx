import { ArrowLeft, Pencil, Pin, PinOff, Plus, RefreshCw, Search, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { AuthError, request } from "../api";
import { Markdown } from "../components/Markdown";

const TYPES: { value: string; label: string }[] = [
  { value: "person", label: "Personen" },
  { value: "project", label: "Projekte" },
  { value: "organization", label: "Organisationen" },
  { value: "idea", label: "Ideen" },
  { value: "strategy", label: "Strategien" },
  { value: "goal", label: "Ziele" },
  { value: "life_area", label: "Lebensbereiche" },
  { value: "topic", label: "Themen" },
  { value: "self", label: "Ich" },
];

interface Fact {
  id: number;
  content: string;
  category: string;
  importance: number;
  status: string;
  pinned: boolean;
  created_at: string;
  entities: { id: number; name: string; type: string }[];
}

interface Entity {
  id: number;
  type: string;
  name: string;
  aliases: string[];
  summary: string;
  attributes: Record<string, unknown>;
  status: string;
  fact_count?: number;
  updated_at: string;
}

const fmtDate = (iso?: string | null) => (iso ? new Date(iso).toLocaleDateString("de-DE") : "");

function useLoader<T>(fn: () => Promise<T>, deps: unknown[], onAuthError: () => void) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const reload = useCallback(() => {
    fn()
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e) => (e instanceof AuthError ? onAuthError() : setError(e.message)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  useEffect(reload, [reload]);
  return { data, error, reload, setData };
}

function FactRow({ fact, onChanged }: { fact: Fact; onChanged: () => void }) {
  const inactive = fact.status !== "active";
  return (
    <div className={`fact-row${inactive ? " inactive" : ""}`}>
      <div className="fact-text">
        {fact.content}
        <div className="fact-meta">
          {fmtDate(fact.created_at)} · {fact.category}
          {fact.status === "superseded" && " · abgelöst"}
          {fact.status === "deleted" && " · gelöscht"}
        </div>
      </div>
      {!inactive && (
        <div className="row-actions">
          <button
            className="icon-btn small"
            title={fact.pinned ? "Nicht mehr in jedem Chat zeigen" : "Als gespeicherte Erinnerung in jeden Chat"}
            onClick={async () => {
              await request(fact.pinned ? "DELETE" : "POST", `/memory/facts/${fact.id}/pin`);
              onChanged();
            }}
          >
            {fact.pinned ? <PinOff size={14} /> : <Pin size={14} />}
          </button>
          <button
            className="icon-btn small"
            title="Löschen"
            onClick={async () => {
              await request("DELETE", `/memory/facts/${fact.id}`);
              onChanged();
            }}
          >
            <Trash2 size={14} />
          </button>
        </div>
      )}
    </div>
  );
}

function Overview({ onAuthError }: { onAuthError: () => void }) {
  const profile = useLoader(() => request("GET", "/memory/profile"), [], onAuthError);
  const stats = useLoader(() => request("GET", "/memory/stats"), [], onAuthError);
  const [editing, setEditing] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const p: any = profile.data;
  const st: any = stats.data;
  const count = (o: Record<string, number> | undefined) => (o ? Object.values(o).reduce((a, b) => a + b, 0) : 0);

  return (
    <div className="mem-section">
      {st && (
        <div className="stats">
          <div className="stat">
            <b>{st.facts?.active ?? 0}</b> Fakten
          </div>
          <div className="stat">
            <b>{count(st.entities) - (st.entities?.self ?? 0)}</b> Einträge
          </div>
          <div className="stat">
            <b>{st.conversations}</b> Chats
          </div>
          <div className="stat">
            <b>{st.pending_jobs}</b> in Verarbeitung
          </div>
        </div>
      )}
      <div className="card">
        <div className="card-head">
          <h3>Memory-Zusammenfassung</h3>
          <span className="muted small">
            {p?.updated_at ? `aktualisiert ${fmtDate(p.updated_at)}` : "noch nicht erstellt"}
          </span>
          <span className="grow" />
          {editing === null ? (
            <>
              <button className="btn small" onClick={() => setEditing(p?.content || "")}>
                <Pencil size={14} /> Bearbeiten
              </button>
              <button
                className="btn small"
                disabled={busy}
                onClick={async () => {
                  setBusy(true);
                  try {
                    await request("POST", "/memory/profile/rebuild");
                    profile.reload();
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                <RefreshCw size={14} className={busy ? "spin" : ""} /> Neu erstellen
              </button>
            </>
          ) : (
            <>
              <button className="btn small" onClick={() => setEditing(null)}>
                Abbrechen
              </button>
              <button
                className="btn small primary"
                onClick={async () => {
                  await request("PUT", "/memory/profile", { content: editing });
                  setEditing(null);
                  profile.reload();
                }}
              >
                Speichern
              </button>
            </>
          )}
        </div>
        {editing !== null ? (
          <textarea className="input mono tall" value={editing} onChange={(e) => setEditing(e.target.value)} />
        ) : p?.content ? (
          <Markdown text={p.content} />
        ) : (
          <p className="muted">
            Die Zusammenfassung entsteht automatisch, sobald ein paar Gespräche verarbeitet wurden. Sie steht in jedem
            Chat und enthält auch, wie du Antworten am liebsten hast.
          </p>
        )}
      </div>
    </div>
  );
}

function Saved({ onAuthError }: { onAuthError: () => void }) {
  const saved = useLoader<Fact[]>(() => request("GET", "/memory/saved"), [], onAuthError);
  const [text, setText] = useState("");
  return (
    <div className="mem-section">
      <p className="muted">
        Gespeicherte Erinnerungen stehen in <b>jedem</b> Chat. Sie entstehen, wenn du „Merk dir …“ sagst – oder du legst
        sie hier an.
      </p>
      <form
        className="add-row"
        onSubmit={async (e) => {
          e.preventDefault();
          if (!text.trim()) return;
          await request("POST", "/memory/saved", { content: text.trim(), importance: 4 });
          setText("");
          saved.reload();
        }}
      >
        <input
          className="input"
          placeholder="z. B. Antworte mir bitte immer kurz und auf Deutsch"
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        <button className="btn primary" disabled={!text.trim()}>
          <Plus size={15} /> Merken
        </button>
      </form>
      <div className="card list">
        {saved.data?.length ? (
          saved.data.map((f) => <FactRow key={f.id} fact={f} onChanged={saved.reload} />)
        ) : (
          <div className="muted pad">Noch keine gespeicherten Erinnerungen.</div>
        )}
      </div>
    </div>
  );
}

function EntityDetail({ id, onBack, onAuthError }: { id: number; onBack: () => void; onAuthError: () => void }) {
  const ent = useLoader<any>(() => request("GET", `/memory/entities/${id}`), [id], onAuthError);
  const [history, setHistory] = useState(false);
  const [summary, setSummary] = useState<string | null>(null);
  const e = ent.data;
  if (!e) return <div className="muted pad">{ent.error || "Lädt …"}</div>;
  const facts: Fact[] = (e.facts || []).filter((f: Fact) => history || f.status === "active");
  return (
    <div className="mem-section">
      <button className="btn ghost small" onClick={onBack}>
        <ArrowLeft size={15} /> Zurück
      </button>
      <div className="card">
        <div className="card-head">
          <h2>{e.name}</h2>
          <span className="badge">{TYPES.find((t) => t.value === e.type)?.label ?? e.type}</span>
          {e.status !== "active" && <span className="badge">{e.status}</span>}
          <span className="grow" />
          {e.type !== "self" && (
            <button
              className="btn small danger-ghost"
              onClick={async () => {
                if (!confirm(`„${e.name}“ löschen? Die Fakten bleiben erhalten, verlieren aber die Zuordnung.`)) return;
                await request("DELETE", `/memory/entities/${id}`);
                onBack();
              }}
            >
              <Trash2 size={14} /> Löschen
            </button>
          )}
        </div>
        {e.aliases?.length > 0 && <div className="muted small">Auch: {e.aliases.join(", ")}</div>}
        {summary === null ? (
          <p className="entity-summary" onClick={() => setSummary(e.summary || "")} title="Klicken zum Bearbeiten">
            {e.summary || <span className="muted">Keine Zusammenfassung – klicken zum Ergänzen.</span>}
          </p>
        ) : (
          <div className="edit-block">
            <textarea className="input" value={summary} onChange={(ev) => setSummary(ev.target.value)} autoFocus />
            <div className="edit-actions">
              <button className="btn small" onClick={() => setSummary(null)}>
                Abbrechen
              </button>
              <button
                className="btn small primary"
                onClick={async () => {
                  await request("PATCH", `/memory/entities/${id}`, { summary });
                  setSummary(null);
                  ent.reload();
                }}
              >
                Speichern
              </button>
            </div>
          </div>
        )}
        {Object.keys(e.attributes || {}).length > 0 && (
          <div className="attrs">
            {Object.entries(e.attributes).map(([k, v]) => (
              <span key={k} className="attr">
                <span className="muted">{k}</span> {String(v)}
              </span>
            ))}
          </div>
        )}
        {e.relations?.length > 0 && (
          <div className="relations">
            {e.relations.map((r: any) => (
              <span key={r.id} className="relation">
                {r.source_id === e.id ? (
                  <>
                    {r.type.replace(/_/g, " ")} → <b>{r.target_name}</b>
                  </>
                ) : (
                  <>
                    <b>{r.source_name}</b> {r.type.replace(/_/g, " ")} → {e.name}
                  </>
                )}
              </span>
            ))}
          </div>
        )}
      </div>
      <div className="card-head">
        <h3>Fakten</h3>
        <span className="grow" />
        <label className="toggle small">
          <input type="checkbox" checked={history} onChange={(ev) => setHistory(ev.target.checked)} /> Historie zeigen
        </label>
      </div>
      <div className="card list">
        {facts.length ? (
          facts.map((f) => <FactRow key={f.id} fact={f} onChanged={ent.reload} />)
        ) : (
          <div className="muted pad">Keine Fakten.</div>
        )}
      </div>
    </div>
  );
}

function Knowledge({ onAuthError }: { onAuthError: () => void }) {
  const [type, setType] = useState("person");
  const [q, setQ] = useState("");
  const [selected, setSelected] = useState<number | null>(null);
  const list = useLoader<Entity[]>(
    () => request("GET", `/memory/entities?type=${type}${q ? `&q=${encodeURIComponent(q)}` : ""}`),
    [type, q],
    onAuthError,
  );
  if (selected !== null)
    return (
      <EntityDetail
        id={selected}
        onAuthError={onAuthError}
        onBack={() => {
          setSelected(null);
          list.reload();
        }}
      />
    );
  return (
    <div className="mem-section">
      <div className="chips">
        {TYPES.map((t) => (
          <button key={t.value} className={`chip${t.value === type ? " active" : ""}`} onClick={() => setType(t.value)}>
            {t.label}
          </button>
        ))}
      </div>
      <div className="search boxed">
        <Search size={15} />
        <input placeholder="Suchen" value={q} onChange={(e) => setQ(e.target.value)} />
        {q && (
          <button className="icon-btn small" onClick={() => setQ("")}>
            <X size={14} />
          </button>
        )}
      </div>
      <div className="entity-grid">
        {list.data?.map((e) => (
          <button key={e.id} className={`entity-card${e.status !== "active" ? " inactive" : ""}`} onClick={() => setSelected(e.id)}>
            <div className="entity-name">{e.name}</div>
            <div className="entity-sum">{e.summary || <span className="muted">—</span>}</div>
            <div className="muted small">
              {e.fact_count ?? 0} Fakten · {fmtDate(e.updated_at)}
            </div>
          </button>
        ))}
        {list.data && !list.data.length && <div className="muted pad">Noch nichts gespeichert.</div>}
      </div>
    </div>
  );
}

export function MemoryPage({ onAuthError }: { onAuthError: () => void }) {
  const [tab, setTab] = useState<"overview" | "saved" | "knowledge">("overview");
  return (
    <div className="page">
      <header className="page-header">
        <h1>Gedächtnis</h1>
        <div className="tabs">
          <button className={tab === "overview" ? "active" : ""} onClick={() => setTab("overview")}>
            Zusammenfassung
          </button>
          <button className={tab === "saved" ? "active" : ""} onClick={() => setTab("saved")}>
            Gespeichert
          </button>
          <button className={tab === "knowledge" ? "active" : ""} onClick={() => setTab("knowledge")}>
            Wissen
          </button>
        </div>
      </header>
      <div className="page-body">
        {tab === "overview" && <Overview onAuthError={onAuthError} />}
        {tab === "saved" && <Saved onAuthError={onAuthError} />}
        {tab === "knowledge" && <Knowledge onAuthError={onAuthError} />}
      </div>
    </div>
  );
}

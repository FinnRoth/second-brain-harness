"""Datenzugriff für Gedächtnis, Chats, Profil und Jobs."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from ..db import Database, utcnow

ENTITY_TYPES = (
    "self",
    "person",
    "project",
    "organization",
    "idea",
    "strategy",
    "goal",
    "life_area",
    "topic",
)

_STOPWORDS = set(
    """
    aber alle allem allen aller alles als also am an ander andere anderem anderen anderer anderes auch auf aus
    bei bin bis bist da damit dann das dass dein deine dem den denn der des dich die dies diese diesem diesen
    dieser dieses dir doch dort du durch ein eine einem einen einer eines er es etwas euch euer für gegen
    gewesen hab habe haben hat hatte hatten hier hin hinter ich ihm ihn ihnen ihr ihre im in indem ins ist
    jede jedem jeden jeder jedes jetzt kann kannst kein keine können könnte machen man manche mein meine mich
    mir mit muss musst nach nicht nichts noch nun nur ob oder ohne sehr sein seine sich sie sind so solche
    soll sollte sondern sonst über um und uns unser unter viel vom von vor war waren warst was weg weil
    weiter welche wenn wer werde werden wie wieder will wir wird wirst wo wollen wollte würde würden zu zum
    zur zwar zwischen mal gerade bitte danke okay ok ja nein hallo hey hi gibt geht gut mehr wieder schon
    the and for are but not you all any can had her was one our out has have how its may new now see two way
    who did get let say she too use what when where which with this that from they will would there their
    """.split()
)


def fts_query(text: str, max_terms: int = 16) -> str | None:
    words = re.findall(r"\w{3,}", text.lower())
    seen: list[str] = []
    for w in words:
        if w in _STOPWORDS or w.isdigit() or w in seen:
            continue
        seen.append(w)
    if not seen:
        return None
    seen.sort(key=len, reverse=True)
    return " OR ".join(f'"{w}"' for w in seen[:max_terms])


def _normalize(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.lower()))


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _entity(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "type": row["type"],
        "name": row["name"],
        "aliases": _loads(row["aliases"], []),
        "summary": row["summary"],
        "attributes": _loads(row["attributes"], {}),
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _fact(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "content": row["content"],
        "category": row["category"],
        "importance": row["importance"],
        "confidence": row["confidence"],
        "status": row["status"],
        "pinned": bool(row["pinned"]),
        "superseded_by": row["superseded_by"],
        "source_conversation_id": row["source_conversation_id"],
        "source_turn_id": row["source_turn_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_accessed_at": row["last_accessed_at"],
        "access_count": row["access_count"],
    }


class MemoryStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ Entities
    def ensure_self(self, user_name: str) -> int:
        row = self.db.query_one("SELECT id, name FROM entities WHERE type='self' ORDER BY id LIMIT 1")
        if row:
            if row["name"] != user_name and user_name:
                self.db.execute(
                    "UPDATE entities SET name=?, updated_at=? WHERE id=?", (user_name, utcnow(), row["id"])
                )
            return row["id"]
        return self.create_entity("self", user_name or "Nutzer", summary="Der Nutzer selbst.")

    def self_id(self) -> int:
        return self.db.scalar("SELECT id FROM entities WHERE type='self' ORDER BY id LIMIT 1")

    def create_entity(
        self,
        type: str,
        name: str,
        aliases: Iterable[str] = (),
        summary: str = "",
        attributes: dict[str, Any] | None = None,
        status: str = "active",
    ) -> int:
        now = utcnow()
        aliases = [a for a in dict.fromkeys(a.strip() for a in aliases if a and a.strip()) if a != name]
        cur = self.db.execute(
            "INSERT INTO entities (type, name, aliases, summary, attributes, status, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                type if type in ENTITY_TYPES else "topic",
                name.strip(),
                json.dumps(aliases, ensure_ascii=False),
                summary.strip(),
                json.dumps(attributes or {}, ensure_ascii=False),
                status,
                now,
                now,
            ),
        )
        return cur.lastrowid

    def get_entity(self, entity_id: int) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM entities WHERE id=?", (entity_id,))
        return _entity(row) if row else None

    def find_entity_by_name(self, name: str, type: str | None = None) -> dict[str, Any] | None:
        needle = name.strip().lower()
        if not needle:
            return None
        rows = self.db.query("SELECT * FROM entities" + (" WHERE type=?" if type else ""), (type,) if type else ())
        alias_hit = None
        for row in rows:
            ent = _entity(row)
            if ent["name"].lower() == needle:
                return ent
            if alias_hit is None and needle in (a.lower() for a in ent["aliases"]):
                alias_hit = ent
        return alias_hit

    def resolve_entity(self, ref: str | int) -> dict[str, Any] | None:
        """Findet eine Entität per ID ("12", "E12") oder Name/Alias."""
        if isinstance(ref, int):
            return self.get_entity(ref)
        s = str(ref).strip()
        m = re.fullmatch(r"[Ee]?(\d+)", s)
        if m:
            ent = self.get_entity(int(m.group(1)))
            if ent:
                return ent
        return self.find_entity_by_name(s)

    def list_entities(
        self, type: str | None = None, status: str | None = None, q: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM entities WHERE 1=1"
        params: list[Any] = []
        if type:
            sql += " AND type=?"
            params.append(type)
        if status:
            sql += " AND status=?"
            params.append(status)
        if q:
            sql += " AND (name LIKE ? OR aliases LIKE ? OR summary LIKE ?)"
            params += [f"%{q}%"] * 3
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        return [_entity(r) for r in self.db.query(sql, tuple(params))]

    def update_entity(
        self,
        entity_id: int,
        *,
        name: str | None = None,
        type: str | None = None,
        aliases: Iterable[str] | None = None,
        summary: str | None = None,
        attributes: dict[str, Any] | None = None,
        status: str | None = None,
        replace_attributes: bool = False,
    ) -> dict[str, Any] | None:
        ent = self.get_entity(entity_id)
        if not ent:
            return None
        new_name = name.strip() if name and name.strip() else ent["name"]
        merged_aliases = list(ent["aliases"])
        if new_name != ent["name"]:
            merged_aliases.append(ent["name"])
        if aliases:
            merged_aliases += [a.strip() for a in aliases if a and a.strip()]
        merged_aliases = [a for a in dict.fromkeys(merged_aliases) if a.lower() != new_name.lower()]
        attrs = dict(ent["attributes"]) if not replace_attributes else {}
        if attributes:
            for k, v in attributes.items():
                if v is None or v == "":
                    attrs.pop(k, None)
                else:
                    attrs[k] = v
        self.db.execute(
            "UPDATE entities SET name=?, type=?, aliases=?, summary=?, attributes=?, status=?, updated_at=?,"
            " embedding = CASE WHEN ? THEN NULL ELSE embedding END WHERE id=?",
            (
                new_name,
                type if type in ENTITY_TYPES else ent["type"],
                json.dumps(merged_aliases, ensure_ascii=False),
                summary.strip() if summary is not None else ent["summary"],
                json.dumps(attrs, ensure_ascii=False),
                status or ent["status"],
                utcnow(),
                1 if (summary is not None or name) else 0,
                entity_id,
            ),
        )
        return self.get_entity(entity_id)

    def delete_entity(self, entity_id: int) -> bool:
        return self.db.execute("DELETE FROM entities WHERE id=? AND type!='self'", (entity_id,)).rowcount > 0

    def merge_entities(self, keep_id: int, merge_id: int) -> dict[str, Any] | None:
        keep, merge = self.get_entity(keep_id), self.get_entity(merge_id)
        if not keep or not merge or keep_id == merge_id:
            return None
        with self.db.transaction():
            self.db.execute(
                "INSERT OR IGNORE INTO fact_entities (fact_id, entity_id)"
                " SELECT fact_id, ? FROM fact_entities WHERE entity_id=?",
                (keep_id, merge_id),
            )
            for col, other in (("source_id", "target_id"), ("target_id", "source_id")):
                self.db.execute(
                    f"UPDATE OR IGNORE relations SET {col}=? WHERE {col}=? AND {other}!=?",
                    (keep_id, merge_id, keep_id),
                )
            self.update_entity(
                keep_id,
                aliases=[merge["name"], *merge["aliases"]],
                attributes={**merge["attributes"], **keep["attributes"]},
            )
            self.db.execute("DELETE FROM entities WHERE id=?", (merge_id,))
        return self.get_entity(keep_id)

    def entity_name_index(self) -> list[tuple[int, str, list[str]]]:
        rows = self.db.query("SELECT id, type, name, aliases FROM entities WHERE status='active'")
        return [(r["id"], r["type"], [r["name"], *_loads(r["aliases"], [])]) for r in rows]

    def set_entity_embedding(self, entity_id: int, blob: bytes) -> None:
        self.db.execute("UPDATE entities SET embedding=? WHERE id=?", (blob, entity_id))

    def entity_embeddings(self) -> list[tuple[int, bytes]]:
        rows = self.db.query("SELECT id, embedding FROM entities WHERE embedding IS NOT NULL AND status='active'")
        return [(r["id"], r["embedding"]) for r in rows]

    def entities_without_embedding(self) -> list[dict[str, Any]]:
        return [_entity(r) for r in self.db.query("SELECT * FROM entities WHERE embedding IS NULL")]

    def entity_fact_counts(self) -> dict[int, int]:
        rows = self.db.query(
            "SELECT fe.entity_id, COUNT(*) AS n FROM fact_entities fe JOIN facts f ON f.id=fe.fact_id"
            " WHERE f.status='active' GROUP BY fe.entity_id"
        )
        return {r["entity_id"]: r["n"] for r in rows}

    # --------------------------------------------------------------------- Facts
    def add_fact(
        self,
        content: str,
        entity_ids: Iterable[int] = (),
        category: str = "sonstiges",
        importance: int = 3,
        confidence: float = 1.0,
        source_conversation_id: str | None = None,
        source_turn_id: int | None = None,
        pinned: bool = False,
    ) -> int:
        now = utcnow()
        with self.db.transaction():
            cur = self.db.execute(
                "INSERT INTO facts (content, category, importance, confidence, source_conversation_id,"
                " source_turn_id, pinned, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    content.strip(),
                    (category or "sonstiges").strip().lower(),
                    max(1, min(5, int(importance))),
                    confidence,
                    source_conversation_id,
                    source_turn_id,
                    1 if pinned else 0,
                    now,
                    now,
                ),
            )
            fact_id = cur.lastrowid
            self.link_fact_entities(fact_id, entity_ids)
        return fact_id

    def link_fact_entities(self, fact_id: int, entity_ids: Iterable[int]) -> None:
        for eid in set(entity_ids):
            self.db.execute("INSERT OR IGNORE INTO fact_entities (fact_id, entity_id) VALUES (?,?)", (fact_id, eid))

    def supersede_fact(
        self,
        old_id: int,
        new_content: str,
        entity_ids: Iterable[int] | None = None,
        category: str | None = None,
        importance: int | None = None,
        source_conversation_id: str | None = None,
        source_turn_id: int | None = None,
    ) -> int | None:
        old = self.get_fact(old_id)
        if not old or old["status"] != "active":
            return None
        with self.db.transaction():
            ids = set(entity_ids or ()) | {e["id"] for e in old["entities"]}
            new_id = self.add_fact(
                new_content,
                ids,
                category=category or old["category"],
                importance=importance if importance is not None else old["importance"],
                source_conversation_id=source_conversation_id,
                source_turn_id=source_turn_id,
                pinned=old["pinned"],
            )
            self.db.execute(
                "UPDATE facts SET status='superseded', superseded_by=?, updated_at=? WHERE id=?",
                (new_id, utcnow(), old_id),
            )
        return new_id

    def delete_fact(self, fact_id: int, hard: bool = False) -> bool:
        if hard:
            return self.db.execute("DELETE FROM facts WHERE id=?", (fact_id,)).rowcount > 0
        return (
            self.db.execute(
                "UPDATE facts SET status='deleted', updated_at=? WHERE id=? AND status='active'",
                (utcnow(), fact_id),
            ).rowcount
            > 0
        )

    def edit_fact(
        self,
        fact_id: int,
        content: str | None = None,
        category: str | None = None,
        importance: int | None = None,
        entity_ids: Iterable[int] | None = None,
    ) -> dict[str, Any] | None:
        fact = self.get_fact(fact_id)
        if not fact:
            return None
        with self.db.transaction():
            self.db.execute(
                "UPDATE facts SET content=?, category=?, importance=?, updated_at=?,"
                " embedding = CASE WHEN ? THEN NULL ELSE embedding END WHERE id=?",
                (
                    content.strip() if content else fact["content"],
                    category or fact["category"],
                    max(1, min(5, int(importance))) if importance is not None else fact["importance"],
                    utcnow(),
                    1 if content else 0,
                    fact_id,
                ),
            )
            if entity_ids is not None:
                self.db.execute("DELETE FROM fact_entities WHERE fact_id=?", (fact_id,))
                self.link_fact_entities(fact_id, entity_ids)
        return self.get_fact(fact_id)

    def _attach_entities(self, facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not facts:
            return facts
        ids = [f["id"] for f in facts]
        rows = self.db.query(
            f"SELECT fe.fact_id, e.id, e.name, e.type FROM fact_entities fe JOIN entities e ON e.id=fe.entity_id"
            f" WHERE fe.fact_id IN ({','.join('?' * len(ids))})",
            tuple(ids),
        )
        by_fact: dict[int, list[dict[str, Any]]] = {}
        for r in rows:
            by_fact.setdefault(r["fact_id"], []).append({"id": r["id"], "name": r["name"], "type": r["type"]})
        for f in facts:
            f["entities"] = by_fact.get(f["id"], [])
        return facts

    def get_fact(self, fact_id: int) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM facts WHERE id=?", (fact_id,))
        return self._attach_entities([_fact(row)])[0] if row else None

    def get_facts(self, ids: Iterable[int], active_only: bool = True) -> list[dict[str, Any]]:
        ids = list(dict.fromkeys(ids))
        if not ids:
            return []
        sql = f"SELECT * FROM facts WHERE id IN ({','.join('?' * len(ids))})"
        if active_only:
            sql += " AND status='active'"
        by_id = {r["id"]: _fact(r) for r in self.db.query(sql, tuple(ids))}
        return self._attach_entities([by_id[i] for i in ids if i in by_id])

    def list_facts(
        self,
        entity_id: int | None = None,
        q: str | None = None,
        include_inactive: bool = False,
        min_importance: int = 1,
        order: str = "recent",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        sql = "SELECT f.* FROM facts f"
        params: list[Any] = []
        if entity_id is not None:
            sql += " JOIN fact_entities fe ON fe.fact_id=f.id AND fe.entity_id=?"
            params.append(entity_id)
        sql += " WHERE f.importance>=?"
        params.append(min_importance)
        if not include_inactive:
            sql += " AND f.status='active'"
        if q:
            sql += " AND f.content LIKE ?"
            params.append(f"%{q}%")
        sql += (
            " ORDER BY f.importance DESC, f.updated_at DESC"
            if order == "importance"
            else " ORDER BY f.created_at DESC, f.id DESC"
        )
        sql += " LIMIT ?"
        params.append(limit)
        return self._attach_entities([_fact(r) for r in self.db.query(sql, tuple(params))])

    def search_facts_fts(self, text: str, limit: int = 40) -> list[tuple[int, float]]:
        q = fts_query(text)
        if not q:
            return []
        try:
            rows = self.db.query(
                "SELECT f.id, bm25(facts_fts) AS score FROM facts_fts JOIN facts f ON f.id=facts_fts.rowid"
                " WHERE facts_fts MATCH ? AND f.status='active' ORDER BY score LIMIT ?",
                (q, limit),
            )
        except Exception:
            return []
        return [(r["id"], r["score"]) for r in rows]

    def pinned_facts(self) -> list[dict[str, Any]]:
        """Gespeicherte Erinnerungen (explizit gemerkt), wichtigste/neueste zuerst."""
        rows = self.db.query(
            "SELECT * FROM facts WHERE pinned=1 AND status='active' ORDER BY importance DESC, created_at DESC"
        )
        return self._attach_entities([_fact(r) for r in rows])

    def set_pinned(self, fact_id: int, pinned: bool) -> bool:
        return (
            self.db.execute(
                "UPDATE facts SET pinned=?, updated_at=? WHERE id=? AND status='active'",
                (1 if pinned else 0, utcnow(), fact_id),
            ).rowcount
            > 0
        )

    def find_duplicate_fact(self, content: str) -> int | None:
        """Aktiver Fakt mit (normalisiert) identischem Inhalt – Schutz vor Dubletten."""
        norm = _normalize(content)
        if not norm:
            return None
        for fid, _ in self.search_facts_fts(content, limit=15):
            row = self.db.query_one("SELECT content FROM facts WHERE id=?", (fid,))
            if row and _normalize(row["content"]) == norm:
                return fid
        return None

    def fact_embeddings(self) -> list[tuple[int, bytes]]:
        rows = self.db.query("SELECT id, embedding FROM facts WHERE embedding IS NOT NULL AND status='active'")
        return [(r["id"], r["embedding"]) for r in rows]

    def facts_without_embedding(self, limit: int = 500) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM facts WHERE embedding IS NULL AND status='active' LIMIT ?", (limit,)
        )
        return [_fact(r) for r in rows]

    def set_fact_embedding(self, fact_id: int, blob: bytes) -> None:
        self.db.execute("UPDATE facts SET embedding=? WHERE id=?", (blob, fact_id))

    def clear_embeddings(self) -> None:
        with self.db.transaction():
            self.db.execute("UPDATE facts SET embedding=NULL")
            self.db.execute("UPDATE entities SET embedding=NULL")
            self.db.execute("UPDATE conversations SET summary_embedding=NULL")

    def touch_facts(self, ids: Iterable[int]) -> None:
        ids = list(ids)
        if ids:
            self.db.execute(
                f"UPDATE facts SET access_count=access_count+1, last_accessed_at=?"
                f" WHERE id IN ({','.join('?' * len(ids))})",
                (utcnow(), *ids),
            )

    # ----------------------------------------------------------------- Relations
    def upsert_relation(self, source_id: int, target_id: int, type: str, description: str = "") -> int | None:
        if source_id == target_id:
            return None
        now = utcnow()
        type = re.sub(r"\s+", "_", type.strip().lower()) or "bezug"
        self.db.execute(
            "INSERT INTO relations (source_id, target_id, type, description, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT(source_id, target_id, type)"
            " DO UPDATE SET description=CASE WHEN excluded.description!='' THEN excluded.description"
            " ELSE relations.description END, updated_at=excluded.updated_at",
            (source_id, target_id, type, description.strip(), now, now),
        )
        return self.db.scalar(
            "SELECT id FROM relations WHERE source_id=? AND target_id=? AND type=?", (source_id, target_id, type)
        )

    def relations_for_entity(self, entity_id: int) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT r.*, s.name AS source_name, s.type AS source_type, t.name AS target_name, t.type AS target_type"
            " FROM relations r JOIN entities s ON s.id=r.source_id JOIN entities t ON t.id=r.target_id"
            " WHERE r.source_id=? OR r.target_id=? ORDER BY r.updated_at DESC",
            (entity_id, entity_id),
        )
        return [dict(r) for r in rows]

    def delete_relation(self, relation_id: int) -> bool:
        return self.db.execute("DELETE FROM relations WHERE id=?", (relation_id,)).rowcount > 0

    # ------------------------------------------------------ Conversations/Turns
    def upsert_conversation(self, conversation_id: str, mode: str) -> None:
        now = utcnow()
        self.db.execute(
            "INSERT INTO conversations (id, mode, created_at, updated_at) VALUES (?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at",
            (conversation_id, mode, now, now),
        )

    def upsert_turn(
        self,
        conversation_id: str,
        turn_index: int,
        user_message: str,
        assistant_message: str,
        model: str | None,
        tool_log: list[dict[str, Any]] | None = None,
    ) -> int:
        now = utcnow()
        self.db.execute(
            "INSERT INTO turns (conversation_id, turn_index, user_message, assistant_message, model, tool_log,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(conversation_id, turn_index) DO UPDATE SET user_message=excluded.user_message,"
            " assistant_message=excluded.assistant_message, model=excluded.model, tool_log=excluded.tool_log,"
            " extracted_at=NULL, updated_at=excluded.updated_at",
            (
                conversation_id,
                turn_index,
                user_message,
                assistant_message,
                model,
                json.dumps(tool_log or [], ensure_ascii=False),
                now,
                now,
            ),
        )
        return self.db.scalar(
            "SELECT id FROM turns WHERE conversation_id=? AND turn_index=?", (conversation_id, turn_index)
        )

    def get_turn(self, turn_id: int) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM turns WHERE id=?", (turn_id,))
        if not row:
            return None
        d = dict(row)
        d["tool_log"] = _loads(d["tool_log"], [])
        return d

    def turns_for_conversation(self, conversation_id: str, before_index: int | None = None, limit: int = 200):
        sql = "SELECT * FROM turns WHERE conversation_id=?"
        params: list[Any] = [conversation_id]
        if before_index is not None:
            sql += " AND turn_index<?"
            params.append(before_index)
        sql += " ORDER BY turn_index DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in reversed(self.db.query(sql, tuple(params)))]

    def unextracted_turns(self, conversation_id: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM turns WHERE conversation_id=? AND extracted_at IS NULL ORDER BY turn_index",
            (conversation_id,),
        )
        out = []
        for r in rows:
            d = dict(r)
            d["tool_log"] = _loads(d["tool_log"], [])
            out.append(d)
        return out

    def mark_conversation_dreamed(self, conversation_id: str) -> None:
        self.db.execute("UPDATE conversations SET dreamed_at=? WHERE id=?", (utcnow(), conversation_id))

    def recent_conversations(
        self, exclude_id: str | None = None, limit: int = 25, messages_per_chat: int = 3
    ) -> list[dict[str, Any]]:
        """Letzte Chats für den Prompt – wie bei ChatGPT nur mit den Nachrichten des Nutzers."""
        rows = self.db.query(
            "SELECT id, title, summary, created_at, updated_at FROM conversations"
            " WHERE mode='brain' AND id != ? ORDER BY updated_at DESC LIMIT ?",
            (exclude_id or "", limit),
        )
        out = []
        for r in rows:
            msgs = self.db.query(
                "SELECT user_message FROM turns WHERE conversation_id=? ORDER BY turn_index LIMIT ?",
                (r["id"], messages_per_chat),
            )
            out.append({**dict(r), "user_messages": [m["user_message"] for m in msgs]})
        return out

    def last_activity(self, exclude_id: str | None = None) -> str | None:
        return self.db.scalar(
            "SELECT MAX(updated_at) FROM conversations WHERE mode='brain' AND id != ?", (exclude_id or "",)
        )

    def get_permission_mode(self, conversation_id: str) -> str | None:
        return self.db.scalar("SELECT permission_mode FROM conversations WHERE id=?", (conversation_id,))

    def set_permission_mode(self, conversation_id: str, mode: str, conv_mode: str = "brain") -> None:
        self.upsert_conversation(conversation_id, conv_mode)
        self.db.execute("UPDATE conversations SET permission_mode=? WHERE id=?", (mode, conversation_id))

    def mark_turn_extracted(self, turn_id: int) -> None:
        self.db.execute("UPDATE turns SET extracted_at=? WHERE id=?", (utcnow(), turn_id))

    def search_turns_fts(self, text: str, limit: int = 8) -> list[dict[str, Any]]:
        q = fts_query(text)
        if not q:
            return []
        try:
            rows = self.db.query(
                "SELECT t.*, c.title AS conversation_title, bm25(turns_fts) AS score FROM turns_fts"
                " JOIN turns t ON t.id=turns_fts.rowid JOIN conversations c ON c.id=t.conversation_id"
                " WHERE turns_fts MATCH ? ORDER BY score LIMIT ?",
                (q, limit),
            )
        except Exception:
            return []
        return [dict(r) for r in rows]

    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM conversations WHERE id=?", (conversation_id,))
        if not row:
            return None
        d = dict(row)
        d.pop("summary_embedding", None)
        return d

    def list_conversations(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT c.id, c.mode, c.title, c.summary, c.created_at, c.updated_at,"
            " (SELECT COUNT(*) FROM turns t WHERE t.conversation_id=c.id) AS turn_count"
            " FROM conversations c ORDER BY c.updated_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    def set_conversation_summary(self, conversation_id: str, title: str, summary: str, blob: bytes | None) -> None:
        self.db.execute(
            "UPDATE conversations SET title=?, summary=?, summary_embedding=?, summarized_at=? WHERE id=?",
            (title, summary, blob, utcnow(), conversation_id),
        )

    def conversation_embeddings(self) -> list[tuple[str, bytes]]:
        rows = self.db.query("SELECT id, summary_embedding FROM conversations WHERE summary_embedding IS NOT NULL")
        return [(r["id"], r["summary_embedding"]) for r in rows]

    def search_conversation_summaries(self, text: str, limit: int = 5) -> list[dict[str, Any]]:
        words = [w for w in re.findall(r"\w{4,}", text.lower()) if w not in _STOPWORDS][:8]
        if not words:
            return []
        clause = " OR ".join(["lower(summary) LIKE ? OR lower(title) LIKE ?"] * len(words))
        params: list[Any] = []
        for w in words:
            params += [f"%{w}%", f"%{w}%"]
        rows = self.db.query(
            f"SELECT id, title, summary, updated_at FROM conversations WHERE summary IS NOT NULL AND ({clause})"
            f" ORDER BY updated_at DESC LIMIT ?",
            (*params, limit),
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ Profile
    def get_profile(self) -> dict[str, Any]:
        return dict(self.db.query_one("SELECT * FROM profile WHERE id=1"))

    def set_profile(self, content: str) -> None:
        self.db.execute(
            "UPDATE profile SET content=?, changes_since_build=0, updated_at=? WHERE id=1", (content.strip(), utcnow())
        )

    def bump_profile_changes(self, n: int) -> int:
        self.db.execute("UPDATE profile SET changes_since_build=changes_since_build+? WHERE id=1", (n,))
        return self.db.scalar("SELECT changes_since_build FROM profile WHERE id=1")

    # --------------------------------------------------------------------- Meta
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        value = self.db.scalar("SELECT value FROM meta WHERE key=?", (key,))
        return value if value is not None else default

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # ---------------------------------------------------------------- Freigaben
    def create_approval(self, conversation_id: str, tool_name: str, arguments: dict[str, Any], description: str) -> int:
        now = utcnow()
        cur = self.db.execute(
            "INSERT INTO approvals (conversation_id, tool_name, arguments, description, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (conversation_id, tool_name, json.dumps(arguments, ensure_ascii=False), description, now, now),
        )
        return cur.lastrowid

    def pending_approvals(self, conversation_id: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM approvals WHERE conversation_id=? AND status='pending' ORDER BY id", (conversation_id,)
        )
        return [{**dict(r), "arguments": _loads(r["arguments"], {})} for r in rows]

    def get_approval(self, approval_id: int) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM approvals WHERE id=?", (approval_id,))
        return {**dict(row), "arguments": _loads(row["arguments"], {})} if row else None

    def resolve_approval(self, approval_id: int, status: str, result: str | None = None) -> None:
        self.db.execute(
            "UPDATE approvals SET status=?, result=?, updated_at=? WHERE id=?",
            (status, result, utcnow(), approval_id),
        )

    # --------------------------------------------------------------------- Jobs
    def enqueue_job(
        self,
        type: str,
        payload: dict[str, Any] | None = None,
        delay_seconds: float = 0,
        dedupe_key: str | None = None,
    ) -> int:
        now = datetime.now(timezone.utc)
        run_after = (now + timedelta(seconds=delay_seconds)).isoformat(timespec="seconds")
        payload_json = json.dumps(payload or {}, ensure_ascii=False)
        with self.db.transaction():
            if dedupe_key:
                existing = self.db.query_one(
                    "SELECT id FROM jobs WHERE dedupe_key=? AND status='pending'", (dedupe_key,)
                )
                if existing:
                    self.db.execute(
                        "UPDATE jobs SET payload=?, run_after=?, updated_at=? WHERE id=?",
                        (payload_json, run_after, utcnow(), existing["id"]),
                    )
                    return existing["id"]
            cur = self.db.execute(
                "INSERT INTO jobs (type, payload, dedupe_key, run_after, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?)",
                (type, payload_json, dedupe_key, run_after, utcnow(), utcnow()),
            )
            return cur.lastrowid

    def claim_next_job(self, ignore_schedule: bool = False) -> dict[str, Any] | None:
        with self.db.transaction():
            sql = "SELECT * FROM jobs WHERE status='pending'"
            params: tuple = ()
            if not ignore_schedule:
                sql += " AND run_after<=?"
                params = (utcnow(),)
            row = self.db.query_one(sql + " ORDER BY run_after, id LIMIT 1", params)
            if not row:
                return None
            self.db.execute(
                "UPDATE jobs SET status='running', attempts=attempts+1, updated_at=? WHERE id=?",
                (utcnow(), row["id"]),
            )
        job = dict(row)
        job["payload"] = _loads(job["payload"], {})
        job["attempts"] += 1
        return job

    def complete_job(self, job_id: int) -> None:
        self.db.execute("UPDATE jobs SET status='done', last_error=NULL, updated_at=? WHERE id=?", (utcnow(), job_id))

    def fail_job(self, job_id: int, error: str, retry_in_seconds: float | None) -> None:
        if retry_in_seconds is None:
            self.db.execute(
                "UPDATE jobs SET status='failed', last_error=?, updated_at=? WHERE id=?",
                (error[:4000], utcnow(), job_id),
            )
        else:
            run_after = (datetime.now(timezone.utc) + timedelta(seconds=retry_in_seconds)).isoformat(
                timespec="seconds"
            )
            self.db.execute(
                "UPDATE jobs SET status='pending', last_error=?, run_after=?, updated_at=? WHERE id=?",
                (error[:4000], run_after, utcnow(), job_id),
            )

    def expedite_jobs(self, type: str, exclude_dedupe_key: str | None = None) -> int:
        """Zieht geplante Jobs eines Typs vor (z. B. Dreaming, wenn ein neuer Chat startet)."""
        return self.db.execute(
            "UPDATE jobs SET run_after=? WHERE type=? AND status='pending' AND COALESCE(dedupe_key,'')!=?",
            (utcnow(), type, exclude_dedupe_key or ""),
        ).rowcount

    def reset_running_jobs(self) -> None:
        self.db.execute("UPDATE jobs SET status='pending' WHERE status='running'")

    def list_jobs(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        sql = "SELECT * FROM jobs" + (" WHERE status=?" if status else "") + " ORDER BY id DESC LIMIT ?"
        rows = self.db.query(sql, (status, limit) if status else (limit,))
        return [{**dict(r), "payload": _loads(r["payload"], {})} for r in rows]

    def cleanup_jobs(self, keep_days: int = 14) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat(timespec="seconds")
        self.db.execute("DELETE FROM jobs WHERE status='done' AND updated_at<?", (cutoff,))

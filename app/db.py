"""SQLite-Zugriff inkl. Schema-Migrationen.

Bewusst ohne ORM: das Schema ist überschaubar, und rohes SQL macht FTS5
(Volltextsuche) und Trigger einfach. Alle Zugriffe laufen über eine
Verbindung im WAL-Modus; ein RLock schützt vor parallelen Transaktionen.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

MIGRATIONS: list[str] = [
    # --- v1 ------------------------------------------------------------------
    """
    CREATE TABLE entities (
        id          INTEGER PRIMARY KEY,
        type        TEXT NOT NULL,
        name        TEXT NOT NULL,
        aliases     TEXT NOT NULL DEFAULT '[]',
        summary     TEXT NOT NULL DEFAULT '',
        attributes  TEXT NOT NULL DEFAULT '{}',
        status      TEXT NOT NULL DEFAULT 'active',
        embedding   BLOB,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );
    CREATE INDEX idx_entities_type ON entities(type);

    CREATE TABLE facts (
        id                     INTEGER PRIMARY KEY,
        content                TEXT NOT NULL,
        category               TEXT NOT NULL DEFAULT 'sonstiges',
        importance             INTEGER NOT NULL DEFAULT 3,
        confidence             REAL NOT NULL DEFAULT 1.0,
        status                 TEXT NOT NULL DEFAULT 'active',
        superseded_by          INTEGER REFERENCES facts(id),
        source_conversation_id TEXT,
        source_turn_id         INTEGER,
        embedding              BLOB,
        access_count           INTEGER NOT NULL DEFAULT 0,
        last_accessed_at       TEXT,
        created_at             TEXT NOT NULL,
        updated_at             TEXT NOT NULL
    );
    CREATE INDEX idx_facts_status ON facts(status);

    CREATE TABLE fact_entities (
        fact_id   INTEGER NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
        entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        PRIMARY KEY (fact_id, entity_id)
    );
    CREATE INDEX idx_fact_entities_entity ON fact_entities(entity_id);

    CREATE TABLE relations (
        id          INTEGER PRIMARY KEY,
        source_id   INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        target_id   INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        type        TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        UNIQUE (source_id, target_id, type)
    );

    CREATE TABLE conversations (
        id                TEXT PRIMARY KEY,
        mode              TEXT NOT NULL DEFAULT 'brain',
        title             TEXT,
        summary           TEXT,
        summary_embedding BLOB,
        summarized_at     TEXT,
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL
    );

    CREATE TABLE turns (
        id                INTEGER PRIMARY KEY,
        conversation_id   TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        turn_index        INTEGER NOT NULL,
        user_message      TEXT NOT NULL,
        assistant_message TEXT NOT NULL DEFAULT '',
        model             TEXT,
        tool_log          TEXT NOT NULL DEFAULT '[]',
        extracted_at      TEXT,
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL,
        UNIQUE (conversation_id, turn_index)
    );

    CREATE TABLE profile (
        id                   INTEGER PRIMARY KEY CHECK (id = 1),
        content              TEXT NOT NULL DEFAULT '',
        changes_since_build  INTEGER NOT NULL DEFAULT 0,
        updated_at           TEXT
    );
    INSERT INTO profile (id, content, changes_since_build) VALUES (1, '', 0);

    CREATE TABLE jobs (
        id          INTEGER PRIMARY KEY,
        type        TEXT NOT NULL,
        payload     TEXT NOT NULL DEFAULT '{}',
        dedupe_key  TEXT,
        status      TEXT NOT NULL DEFAULT 'pending',
        attempts    INTEGER NOT NULL DEFAULT 0,
        last_error  TEXT,
        run_after   TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );
    CREATE INDEX idx_jobs_status ON jobs(status, run_after);

    -- Volltextsuche (Trigram = robuste Teilwortsuche, gut für deutsche Komposita)
    CREATE VIRTUAL TABLE facts_fts USING fts5(
        content, content='facts', content_rowid='id', tokenize='trigram'
    );
    CREATE TRIGGER facts_ai AFTER INSERT ON facts BEGIN
        INSERT INTO facts_fts(rowid, content) VALUES (new.id, new.content);
    END;
    CREATE TRIGGER facts_ad AFTER DELETE ON facts BEGIN
        INSERT INTO facts_fts(facts_fts, rowid, content) VALUES ('delete', old.id, old.content);
    END;
    CREATE TRIGGER facts_au AFTER UPDATE OF content ON facts BEGIN
        INSERT INTO facts_fts(facts_fts, rowid, content) VALUES ('delete', old.id, old.content);
        INSERT INTO facts_fts(rowid, content) VALUES (new.id, new.content);
    END;

    CREATE VIRTUAL TABLE turns_fts USING fts5(
        user_message, assistant_message, content='turns', content_rowid='id', tokenize='trigram'
    );
    CREATE TRIGGER turns_ai AFTER INSERT ON turns BEGIN
        INSERT INTO turns_fts(rowid, user_message, assistant_message)
        VALUES (new.id, new.user_message, new.assistant_message);
    END;
    CREATE TRIGGER turns_ad AFTER DELETE ON turns BEGIN
        INSERT INTO turns_fts(turns_fts, rowid, user_message, assistant_message)
        VALUES ('delete', old.id, old.user_message, old.assistant_message);
    END;
    CREATE TRIGGER turns_au AFTER UPDATE OF user_message, assistant_message ON turns BEGIN
        INSERT INTO turns_fts(turns_fts, rowid, user_message, assistant_message)
        VALUES ('delete', old.id, old.user_message, old.assistant_message);
        INSERT INTO turns_fts(rowid, user_message, assistant_message)
        VALUES (new.id, new.user_message, new.assistant_message);
    END;
    """,
    # --- v2: Memory v2 (gespeicherte Erinnerungen, Dreaming) + Freigaben -------
    """
    ALTER TABLE facts ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;
    CREATE INDEX idx_facts_pinned ON facts(pinned, status);
    ALTER TABLE conversations ADD COLUMN permission_mode TEXT;
    ALTER TABLE conversations ADD COLUMN dreamed_at TEXT;

    CREATE TABLE meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE approvals (
        id              INTEGER PRIMARY KEY,
        conversation_id TEXT NOT NULL,
        tool_name       TEXT NOT NULL,
        arguments       TEXT NOT NULL DEFAULT '{}',
        description     TEXT NOT NULL DEFAULT '',
        status          TEXT NOT NULL DEFAULT 'pending',
        result          TEXT,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    );
    CREATE INDEX idx_approvals_conv ON approvals(conversation_id, status);
    """,
]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            path = str(Path(path).expanduser())
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._tx_depth = 0
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    def _migrate(self) -> None:
        with self.lock:
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            for i in range(version, len(MIGRATIONS)):
                self.conn.executescript("BEGIN;\n" + MIGRATIONS[i] + f"\nPRAGMA user_version={i + 1};\nCOMMIT;")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self.lock:
            if self._tx_depth == 0:
                self.conn.execute("BEGIN IMMEDIATE")
            self._tx_depth += 1
            try:
                yield
            except BaseException:
                self._tx_depth -= 1
                if self._tx_depth == 0:
                    self.conn.execute("ROLLBACK")
                raise
            else:
                self._tx_depth -= 1
                if self._tx_depth == 0:
                    self.conn.execute("COMMIT")

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: tuple | dict = ()) -> Any:
        row = self.query_one(sql, params)
        return row[0] if row else None

    def close(self) -> None:
        with self.lock:
            self.conn.close()

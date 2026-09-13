"""Persistencia en SQLite: proyectos, chats, mensajes y nodos."""

from __future__ import annotations

import sqlite3
import threading
import uuid
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    folder       TEXT NOT NULL,
    model        TEXT NOT NULL DEFAULT 'sonnet',
    instructions TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chats (
    id         TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title      TEXT NOT NULL,
    session_id TEXT,
    file_context TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id         TEXT PRIMARY KEY,
    chat_id    TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    node_id    TEXT,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL DEFAULT '',
    seq        INTEGER NOT NULL,
    cost_usd   REAL,
    error      TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
    id                TEXT PRIMARY KEY,
    chat_id           TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    parent_node_id    TEXT,
    anchor_message_id TEXT NOT NULL,
    anchor_start      INTEGER NOT NULL,
    anchor_end        INTEGER NOT NULL,
    anchor_text       TEXT NOT NULL,
    title             TEXT NOT NULL DEFAULT '',
    session_id        TEXT,
    collapsed         INTEGER NOT NULL DEFAULT 0,
    width             INTEGER,
    height            INTEGER,
    seq               INTEGER NOT NULL,
    created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(chat_id, node_id, seq);
CREATE INDEX IF NOT EXISTS idx_nodes_chat ON nodes(chat_id, seq);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex


class NotFound(LookupError):
    """La entidad pedida no existe."""


class Store:
    def __init__(self, path: Path | str) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        with self._lock:
            self._conn.executescript(SCHEMA)
            # Migracion aditiva para bases creadas antes de que cada chat pudiera
            # elegir su propio material. NULL significa "usar todos los archivos".
            chat_columns = {
                row[1] for row in self._conn.execute("PRAGMA table_info(chats)").fetchall()
            }
            if "file_context" not in chat_columns:
                self._conn.execute("ALTER TABLE chats ADD COLUMN file_context TEXT")
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- helpers internos -------------------------------------------------

    def _exec(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def _one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def _all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def _next_seq(self, sql: str, params: tuple) -> int:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return (row[0] or 0) + 1

    def _update(self, table: str, ident: str, fields: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
        changes = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if changes:
            sets = ", ".join(f"{k} = ?" for k in changes)
            self._exec(f"UPDATE {table} SET {sets} WHERE id = ?", (*changes.values(), ident))
        row = self._one(f"SELECT * FROM {table} WHERE id = ?", (ident,))
        if row is None:
            raise NotFound(f"{table}:{ident}")
        return row

    # --- proyectos --------------------------------------------------------

    def create_project(self, name: str, folder: str, model: str = "sonnet") -> dict[str, Any]:
        ident = new_id()
        self._exec(
            "INSERT INTO projects (id, name, folder, model, instructions, created_at)"
            " VALUES (?, ?, ?, ?, '', ?)",
            (ident, name, folder, model, now()),
        )
        return self.get_project(ident)

    def list_projects(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM projects ORDER BY created_at")

    def get_project(self, ident: str) -> dict[str, Any]:
        row = self._one("SELECT * FROM projects WHERE id = ?", (ident,))
        if row is None:
            raise NotFound(f"project:{ident}")
        return row

    def update_project(self, ident: str, **fields: Any) -> dict[str, Any]:
        return self._update("projects", ident, fields, {"name", "folder", "model", "instructions"})

    def delete_project(self, ident: str) -> None:
        chat_ids = [c["id"] for c in self.list_chats(ident)]
        for chat_id in chat_ids:
            self.delete_chat(chat_id)
        self._exec("DELETE FROM projects WHERE id = ?", (ident,))

    # --- chats ------------------------------------------------------------

    def create_chat(self, project_id: str, title: str = "Nuevo chat") -> dict[str, Any]:
        ident = new_id()
        self._exec(
            "INSERT INTO chats (id, project_id, title, session_id, created_at) VALUES (?, ?, ?, NULL, ?)",
            (ident, project_id, title, now()),
        )
        return self.get_chat(ident)

    def list_chats(self, project_id: str) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM chats WHERE project_id = ? ORDER BY created_at DESC", (project_id,)
        )

    def get_chat(self, ident: str) -> dict[str, Any]:
        row = self._one("SELECT * FROM chats WHERE id = ?", (ident,))
        if row is None:
            raise NotFound(f"chat:{ident}")
        return row

    def update_chat(self, ident: str, **fields: Any) -> dict[str, Any]:
        return self._update("chats", ident, fields, {"title", "session_id"})

    def chat_file_context(self, ident: str) -> list[str] | None:
        """Archivos habilitados o ``None`` cuando el chat usa todo el material."""
        chat = self.get_chat(ident)
        raw = chat.get("file_context")
        if raw is None:
            return None
        try:
            names = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None
        return [str(name) for name in names] if isinstance(names, list) else None

    def set_chat_file_context(self, ident: str, names: list[str] | None) -> list[str] | None:
        self.get_chat(ident)
        raw = None if names is None else json.dumps(list(dict.fromkeys(names)), ensure_ascii=False)
        self._exec("UPDATE chats SET file_context = ? WHERE id = ?", (raw, ident))
        return self.chat_file_context(ident)

    def delete_chat(self, ident: str) -> None:
        self._exec("DELETE FROM messages WHERE chat_id = ?", (ident,))
        self._exec("DELETE FROM nodes WHERE chat_id = ?", (ident,))
        self._exec("DELETE FROM chats WHERE id = ?", (ident,))

    # --- mensajes ---------------------------------------------------------

    def add_message(
        self,
        chat_id: str,
        role: str,
        content: str = "",
        node_id: str | None = None,
        cost_usd: float | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        seq = self._next_seq(
            "SELECT MAX(seq) FROM messages WHERE chat_id = ? AND node_id IS ?", (chat_id, node_id)
        )
        ident = new_id()
        self._exec(
            "INSERT INTO messages (id, chat_id, node_id, role, content, seq, cost_usd, error, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ident, chat_id, node_id, role, content, seq, cost_usd, error, now()),
        )
        return self.get_message(ident)

    def get_message(self, ident: str) -> dict[str, Any]:
        row = self._one("SELECT * FROM messages WHERE id = ?", (ident,))
        if row is None:
            raise NotFound(f"message:{ident}")
        return row

    def update_message(self, ident: str, **fields: Any) -> dict[str, Any]:
        return self._update("messages", ident, fields, {"content", "cost_usd", "error"})

    def delete_message(self, ident: str) -> None:
        self._exec("DELETE FROM messages WHERE id = ?", (ident,))

    def thread_messages(self, chat_id: str, node_id: str | None = None) -> list[dict[str, Any]]:
        """Mensajes de un hilo concreto: el principal (node_id NULL) o el de un nodo."""
        return self._all(
            "SELECT * FROM messages WHERE chat_id = ? AND node_id IS ? ORDER BY seq",
            (chat_id, node_id),
        )

    def all_messages(self, chat_id: str) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM messages WHERE chat_id = ? ORDER BY seq", (chat_id,))

    def main_messages_upto(self, chat_id: str, message_id: str) -> list[dict[str, Any]]:
        """Hilo principal recortado hasta el mensaje dado, incluido.

        Si el mensaje no pertenece al hilo principal (el ancla esta dentro de una
        rama), devuelve el hilo principal completo.
        """
        messages = self.thread_messages(chat_id, None)
        for index, message in enumerate(messages):
            if message["id"] == message_id:
                return messages[: index + 1]
        return messages

    # --- nodos ------------------------------------------------------------

    def create_node(
        self,
        chat_id: str,
        anchor_message_id: str,
        anchor_start: int,
        anchor_end: int,
        anchor_text: str,
        title: str = "",
        parent_node_id: str | None = None,
    ) -> dict[str, Any]:
        seq = self._next_seq("SELECT MAX(seq) FROM nodes WHERE chat_id = ?", (chat_id,))
        ident = new_id()
        self._exec(
            "INSERT INTO nodes (id, chat_id, parent_node_id, anchor_message_id, anchor_start,"
            " anchor_end, anchor_text, title, session_id, collapsed, width, height, seq, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, NULL, NULL, ?, ?)",
            (
                ident,
                chat_id,
                parent_node_id,
                anchor_message_id,
                anchor_start,
                anchor_end,
                anchor_text,
                title,
                seq,
                now(),
            ),
        )
        return self.get_node(ident)

    def get_node(self, ident: str) -> dict[str, Any]:
        row = self._one("SELECT * FROM nodes WHERE id = ?", (ident,))
        if row is None:
            raise NotFound(f"node:{ident}")
        return row

    def list_nodes(self, chat_id: str) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM nodes WHERE chat_id = ? ORDER BY seq", (chat_id,))

    def update_node(self, ident: str, **fields: Any) -> dict[str, Any]:
        return self._update(
            "nodes", ident, fields, {"title", "session_id", "collapsed", "width", "height"}
        )

    def node_chain(self, ident: str) -> list[dict[str, Any]]:
        """Nodos ancestros, del mas lejano al padre directo. No incluye el propio nodo."""
        chain: list[dict[str, Any]] = []
        node = self.get_node(ident)
        parent_id = node["parent_node_id"]
        seen: set[str] = {ident}
        while parent_id and parent_id not in seen:
            seen.add(parent_id)
            parent = self.get_node(parent_id)
            chain.append(parent)
            parent_id = parent["parent_node_id"]
        chain.reverse()
        return chain

    def node_subtree_ids(self, ident: str) -> list[str]:
        """El nodo y todos sus descendientes."""
        rows = self._all(
            "WITH RECURSIVE sub(id) AS ("
            "  SELECT id FROM nodes WHERE id = ?"
            "  UNION ALL"
            "  SELECT n.id FROM nodes n JOIN sub ON n.parent_node_id = sub.id"
            ") SELECT id FROM sub",
            (ident,),
        )
        return [r["id"] for r in rows]

    def delete_node(self, ident: str) -> list[str]:
        """Borra el nodo, sus descendientes y todos sus mensajes."""
        ids = self.node_subtree_ids(ident)
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        self._exec(f"DELETE FROM messages WHERE node_id IN ({marks})", tuple(ids))
        self._exec(f"DELETE FROM nodes WHERE id IN ({marks})", tuple(ids))
        return ids

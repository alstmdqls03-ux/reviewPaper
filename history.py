"""Resumable conversations + their messages.

Stdlib only. Time-dependent methods take an injectable `now` (float epoch) for tests.
Opens the shared production db (app.db) — same file as accounts.py, NOT mastery.db.
"""

import sqlite3
import time
import uuid


def _connect(db_path):
    # ponytail: single shared connection, single event-loop thread (see mastery.py).
    # check_same_thread=False reuses it across async endpoints. Ceiling: one thread /
    # one worker. Upgrade path: per-request connection or a small pool for multi-worker.
    db = sqlite3.connect(db_path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    return db


class History:
    def __init__(self, db_path="app.db"):
        self.db = _connect(db_path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations(
                id TEXT PRIMARY KEY, user_id TEXT, title TEXT,
                created_at REAL, updated_at REAL, deleted INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS messages(
                id TEXT PRIMARY KEY, conv_id TEXT, role TEXT, content TEXT,
                created_at REAL
            );
            """
        )
        self.db.commit()

    def start_conversation(self, user_id, title="", now=None):
        now = time.time() if now is None else now
        conv_id = uuid.uuid4().hex
        self.db.execute(
            """INSERT INTO conversations(id,user_id,title,created_at,updated_at,deleted)
               VALUES(?,?,?,?,?,0)""",
            (conv_id, user_id, title, now, now),
        )
        self.db.commit()
        return conv_id

    def append(self, conv_id, role, content, now=None):
        """Add a message, bump updated_at; auto-title from first user message if empty."""
        now = time.time() if now is None else now
        msg_id = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO messages(id,conv_id,role,content,created_at) VALUES(?,?,?,?,?)",
            (msg_id, conv_id, role, content, now),
        )
        conv = self.db.execute(
            "SELECT title FROM conversations WHERE id=?", (conv_id,)
        ).fetchone()
        if role == "user" and conv is not None and not conv["title"]:
            self.db.execute(
                "UPDATE conversations SET title=?, updated_at=? WHERE id=?",
                (content[:40], now, conv_id),
            )
        else:
            self.db.execute(
                "UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id)
            )
        self.db.commit()
        return msg_id

    def list_conversations(self, user_id):
        """Newest-first, excluding soft-deleted, with message counts."""
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "updated_at": r["updated_at"],
                "msg_count": r["msg_count"],
            }
            for r in self.db.execute(
                """SELECT c.id, c.title, c.updated_at,
                          (SELECT COUNT(*) FROM messages m WHERE m.conv_id=c.id) AS msg_count
                   FROM conversations c
                   WHERE c.user_id=? AND c.deleted=0
                   ORDER BY c.updated_at DESC, c.id""",
                (user_id,),
            )
        ]

    def get_messages(self, conv_id):
        return [
            {"role": r["role"], "content": r["content"], "created_at": r["created_at"]}
            for r in self.db.execute(
                "SELECT role,content,created_at FROM messages WHERE conv_id=? "
                "ORDER BY created_at, id",
                (conv_id,),
            )
        ]

    def rename(self, conv_id, title):
        self.db.execute(
            "UPDATE conversations SET title=? WHERE id=?", (title, conv_id)
        )
        self.db.commit()

    def delete(self, conv_id):
        """Soft delete: set deleted=1 (keeps messages for audit/recovery)."""
        self.db.execute(
            "UPDATE conversations SET deleted=1 WHERE id=?", (conv_id,)
        )
        self.db.commit()


if __name__ == "__main__":
    import tempfile
    import os

    db = os.path.join(tempfile.mkdtemp(), "test.db")
    h = History(db)
    t0 = 1_000_000.0

    conv = h.start_conversation("userA", now=t0)
    h.append(conv, "user", "TEM과 SEM의 차이가 뭐야? 초보자용으로 설명해줘", now=t0 + 1)
    h.append(conv, "assistant", "좋은 질문이에요! ...", now=t0 + 2)

    lst = h.list_conversations("userA")
    assert len(lst) == 1
    assert lst[0]["msg_count"] == 2, lst
    assert lst[0]["title"] == "TEM과 SEM의 차이가 뭐야? 초보자용으로 설명해줘"[:40], lst[0]["title"]

    msgs = h.get_messages(conv)
    assert [m["role"] for m in msgs] == ["user", "assistant"], msgs

    # isolation: another user sees nothing
    assert h.list_conversations("userB") == []

    # soft delete hides it
    h.delete(conv)
    assert h.list_conversations("userA") == []

    print("all history self-checks passed")

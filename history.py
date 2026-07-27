"""Resumable conversations + their messages.

Stdlib only. Time-dependent methods take an injectable `now` (float epoch) for tests.
Opens the shared production db (app.db) — same file as accounts.py, NOT mastery.db.
"""

import json
import sqlite3
import time
import uuid


def _snippet(content, query, width=90):
    """매치 주변만 잘라낸다. 카드 한 줄에 들어갈 만큼만 — 앞뒤는 …로 표시."""
    if not content:
        return ""
    i = content.lower().find(query.lower())
    if i < 0:
        return content[:width].strip()
    start = max(0, i - width // 3)
    end = min(len(content), i + len(query) + width // 2)
    return ("…" if start else "") + content[start:end].strip() + ("…" if end < len(content) else "")


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
        # meta = JSON sidecar per message (citations, used sources). Added after the
        # table shipped, so migrate in place rather than dropping anyone's history.
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(messages)")}
        if "meta" not in cols:
            self.db.execute("ALTER TABLE messages ADD COLUMN meta TEXT")
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

    def append(self, conv_id, role, content, now=None, meta=None):
        """Add a message, bump updated_at; auto-title from first user message if empty.
        `meta` is any JSON-able dict (citations, used sources) replayed with the message."""
        now = time.time() if now is None else now
        msg_id = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO messages(id,conv_id,role,content,created_at,meta) VALUES(?,?,?,?,?,?)",
            (msg_id, conv_id, role, content, now,
             json.dumps(meta, ensure_ascii=False) if meta else None),
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

    def search(self, user_id, query, limit=50):
        """제목 + 메시지 본문으로 대화를 찾는다. 매치된 문장 조각(snippet)을 같이 준다.

        ponytail: FTS5가 아니라 LIKE %q%를 쓴다. 고른 이유 —
        (a) 이 앱의 대화량은 사용자당 수백 건 규모라 LIKE 풀스캔이 수 ms에 끝난다.
            FTS5는 별도 가상 테이블 + 트리거로 messages와 동기화를 유지해야 하고,
            그 동기화가 틀어지면 "있는데 안 나온다"가 되어 조용히 잘못된다.
        (b) FTS5의 unicode61 토크나이저는 한국어를 공백 단위로만 자른다. "메타데이터를"과
            "메타데이터"가 다른 토큰이라 조사가 붙으면 안 잡힌다. LIKE 부분일치가
            오히려 한국어에서 더 잘 맞는다.
        한계 — (1) 인덱스를 안 타므로 대화가 수만 건이 되면 느려진다. 그때 FTS5 +
        트리그램 토크나이저로 간다. (2) 랭킹이 없다. 최근 순으로만 준다.
        (3) 대소문자는 ASCII에서만 무시된다(SQLite 기본 NOCASE 한계).
        """
        q = (query or "").strip()
        if not q:
            return self.list_conversations(user_id)[:limit]
        # 사용자가 친 %와 _는 리터럴이다. 안 막으면 "%" 한 글자가 전체 대화를 다 끌어온다.
        esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{esc}%"
        rows = self.db.execute(
            """SELECT c.id, c.title, c.updated_at,
                      (SELECT COUNT(*) FROM messages m WHERE m.conv_id=c.id) AS msg_count,
                      (SELECT m.content FROM messages m
                        WHERE m.conv_id=c.id AND m.content LIKE ? ESCAPE '\\'
                        ORDER BY m.created_at, m.id LIMIT 1) AS hit
                 FROM conversations c
                WHERE c.user_id=? AND c.deleted=0
                  AND (c.title LIKE ? ESCAPE '\\' OR EXISTS(
                        SELECT 1 FROM messages m WHERE m.conv_id=c.id
                                 AND m.content LIKE ? ESCAPE '\\'))
                ORDER BY c.updated_at DESC, c.id
                LIMIT ?""",
            (like, user_id, like, like, limit),
        )
        out = []
        for r in rows:
            out.append({"id": r["id"], "title": r["title"], "updated_at": r["updated_at"],
                        "msg_count": r["msg_count"],
                        "snippet": _snippet(r["hit"], q)})
        return out

    def get_messages(self, conv_id):
        out = []
        for r in self.db.execute(
            "SELECT role,content,created_at,meta FROM messages WHERE conv_id=? "
            "ORDER BY created_at, id",
            (conv_id,),
        ):
            meta = None
            if r["meta"]:
                try:
                    meta = json.loads(r["meta"])
                except ValueError:  # corrupt sidecar must not sink the whole replay
                    meta = None
            out.append({"role": r["role"], "content": r["content"],
                        "created_at": r["created_at"], "meta": meta})
        return out

    def get_title(self, conv_id):
        """The conversation's title, or "" if it has none / doesn't exist."""
        row = self.db.execute(
            "SELECT title FROM conversations WHERE id=?", (conv_id,)
        ).fetchone()
        return (row["title"] if row else "") or ""

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
    h.append(conv, "assistant", "좋은 질문이에요! ...", now=t0 + 2,
             meta={"citations": [{"title": "Paper A", "start_page": 3,
                                  "cited_text": "TEM transmits..."}],
                   "sources": ["Paper A", "Paper B"]})

    lst = h.list_conversations("userA")
    assert len(lst) == 1
    assert lst[0]["msg_count"] == 2, lst
    assert lst[0]["title"] == "TEM과 SEM의 차이가 뭐야? 초보자용으로 설명해줘"[:40], lst[0]["title"]

    msgs = h.get_messages(conv)
    assert [m["role"] for m in msgs] == ["user", "assistant"], msgs

    assert h.get_title(conv) == lst[0]["title"], h.get_title(conv)
    assert h.get_title("no-such-conversation") == ""

    # meta survives the round trip so replayed answers keep their footnotes
    assert msgs[0]["meta"] is None, "user messages carry no meta"
    assert msgs[1]["meta"]["citations"][0]["start_page"] == 3, msgs[1]["meta"]
    assert msgs[1]["meta"]["sources"] == ["Paper A", "Paper B"]
    # a pre-migration row (meta NULL) still replays
    h.db.execute("INSERT INTO messages(id,conv_id,role,content,created_at) VALUES(?,?,?,?,?)",
                 ("old1", conv, "assistant", "legacy answer", t0 + 3))
    h.db.commit()
    assert h.get_messages(conv)[-1]["meta"] is None

    # ---- 검색: 제목만이 아니라 본문까지 ----
    c2 = h.start_conversation("userA", now=t0 + 10)
    h.append(c2, "user", "퀴즈 난이도 조절돼?", now=t0 + 11)
    h.append(c2, "assistant", "간격반복으로 약한 개념부터 나옵니다", now=t0 + 12)

    # 제목에 없는 단어가 본문에만 있어도 잡힌다 (이게 이 기능의 존재 이유)
    hits = h.search("userA", "간격반복")
    assert [x["id"] for x in hits] == [c2], hits
    assert "간격반복" in hits[0]["snippet"], hits[0]
    assert hits[0]["title"] == "퀴즈 난이도 조절돼?", hits[0]  # 제목은 그대로 온다

    # 제목으로도 잡힌다
    assert [x["id"] for x in h.search("userA", "TEM")] == [conv]
    # 두 대화에 다 있는 단어면 최신순으로 둘 다
    h.append(conv, "user", "간격반복이 뭐야", now=t0 + 13)
    assert [x["id"] for x in h.search("userA", "간격반복")] == [conv, c2]
    # 빈 질의는 전체 목록과 같다
    assert len(h.search("userA", "   ")) == len(h.list_conversations("userA"))
    # 없는 단어는 빈 목록
    assert h.search("userA", "존재하지않는단어") == []
    # 검색도 사용자별로 격리된다
    assert h.search("userB", "간격반복") == []
    # LIKE 와일드카드는 리터럴로 취급된다 (%만 넣어서 전체가 딸려오면 안 된다)
    assert h.search("userA", "%") == [], "'%'가 와일드카드로 샜다"
    assert h.search("userA", "_") == [], "'_'가 와일드카드로 샜다"

    # snippet은 매치 주변만 자른다
    long_conv = h.start_conversation("userA", now=t0 + 20)
    h.append(long_conv, "assistant", "가" * 300 + "표적단어" + "나" * 300, now=t0 + 21)
    sn = h.search("userA", "표적단어")[0]["snippet"]
    assert "표적단어" in sn and len(sn) < 200, (len(sn), sn[:60])
    assert sn.startswith("…") and sn.endswith("…"), sn
    h.delete(long_conv); h.delete(c2)

    # isolation: another user sees nothing
    assert h.list_conversations("userB") == []

    # soft delete hides it
    h.delete(conv)
    assert h.list_conversations("userA") == []

    print("all history self-checks passed")

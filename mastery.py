"""Per-ACCOUNT concept mastery, spaced repetition (SM-2-lite), guided study path, notes+export.

Stdlib only (sqlite3). All time-dependent methods take an injectable `now` (float epoch)
so they're testable without touching the wall clock.

**Storage (2026-07-28)**: lives in app.db, not a separate mastery.db. Two files meant
no foreign keys between progress and the account it belongs to, and analytics.py had to
open both and join them by hand. The split was never a domain boundary — mastery.py
simply shipped before the accounts layer existed.

The key column is `user_id`, not `device_id`. It always held the account id
(app.py's `learner()` resolves device -> account before calling in), but the old name
said otherwise and 34 rows written before the accounts layer really were keyed by raw
device id. Those are folded into their account on migration.

Prerequisite model (from graph.json): an edge `{source, target, relation}` with
relation in {part_of, builds_on, uses} means `source` depends on `target` as a foundation.
So a concept's prerequisites are its outgoing targets via those relations; a concept with
no such outgoing edges is foundational (good cold-start entry point).
"""

import json
import sqlite3
import time
import uuid

# Score thresholds
KNOWN = 0.7          # score >= KNOWN -> "known"
                     # row exists but below -> "learning"; no row -> "new"

# SM-2-lite tuning (interval units are seconds; base is one day)
START_EASE = 2.3
MIN_EASE = 1.3
BASE_INTERVAL = 86400.0        # first correct review schedules ~1 day out
EXPOSURE_CAP = 0.5             # mere-exposure can never push a concept into "known"

PREREQ_RELATIONS = {"part_of", "builds_on", "uses"}


LEGACY_DB = "mastery.db"   # 병합 전 파일. 이관 후에는 안 읽는다 (백업으로 남긴다)

SCHEMA = """
CREATE TABLE IF NOT EXISTS mastery(
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    concept_id TEXT NOT NULL,
    score REAL, ease REAL, interval REAL, reps INTEGER,
    last_reviewed REAL,
    PRIMARY KEY(user_id, concept_id)
);
CREATE TABLE IF NOT EXISTS notes(
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    text TEXT, source TEXT, concept_id TEXT, created_at REAL,
    -- 인용 배열 등 JSON 사이드카. messages.meta와 같은 방식 — 컬럼을 늘리는 대신
    -- 답변에 붙어 있던 각주를 그대로 데리고 온다 (노트가 근거를 잃지 않게)
    meta TEXT,
    -- 어느 대화에서 저장했는지. 없으면 대화 밖에서 만든 노트다.
    -- SET NULL: 대화를 지워도 노트는 남는다 (노트는 대화보다 오래 사는 산출물이다)
    conv_id TEXT REFERENCES conversations(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS quiz_attempts(
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    concept_id TEXT, correct INTEGER, created_at REAL
);
-- 인덱스는 반드시 테이블 뒤에. executescript는 순서대로 실행하므로
-- 앞에 두면 빈 DB에서 "no such table"로 죽는다.
-- mastery는 PK(user_id, concept_id)의 왼쪽 접두사가 WHERE user_id=? 를 이미 탄다.
-- notes/quiz_attempts는 PK가 id라 학습자별 조회가 풀스캔이었다 —
-- dashboard() 한 번이 quiz_attempts를 두 번 훑는다(정답률 + streak).
CREATE INDEX IF NOT EXISTS idx_notes_user ON notes(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_quiz_user ON quiz_attempts(user_id, created_at);
"""


class MasteryStore:
    def __init__(self, db_path="app.db", legacy_path=LEGACY_DB):
        # ponytail: single shared connection so an in-memory (":memory:") db survives
        # across calls in tests. check_same_thread=False lets the async endpoints (all on
        # the single event-loop thread) reuse it; add a lock only for threaded workers.
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        # users(accounts.py) / conversations(history.py)는 남의 테이블이지만 FK 대상이고
        # list_notes가 조인한다. CREATE IF NOT EXISTS라 누가 먼저 돌든 결과가 같아서
        # 모듈 초기화 순서를 신경 쓰지 않아도 된다 (corpus.SOURCES_DDL과 같은 방식).
        self.db.executescript(
            """CREATE TABLE IF NOT EXISTS users(
                   id TEXT PRIMARY KEY, display_name TEXT, created_at REAL);
               CREATE TABLE IF NOT EXISTS conversations(
                   id TEXT PRIMARY KEY, user_id TEXT, title TEXT,
                   created_at REAL, updated_at REAL, deleted INTEGER DEFAULT 0);""")
        self.db.executescript(SCHEMA)
        # conv_id는 notes가 출시된 뒤에 붙었다. 기존 DB는 제자리 이관한다
        # (history.messages.meta 때와 같은 방식). ALTER는 FK 절을 못 붙이므로
        # 옛 DB에서는 컬럼만 생기고 FK는 없다 — 새로 만든 DB에는 SCHEMA대로 FK가 붙는다.
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(notes)")}
        if "conv_id" not in cols:
            self.db.execute("ALTER TABLE notes ADD COLUMN conv_id TEXT")
        if "meta" not in cols:
            self.db.execute("ALTER TABLE notes ADD COLUMN meta TEXT")
        self.db.commit()
        self.migrated = self._migrate_legacy(legacy_path)

    # ---- one-time migration ---------------------------------------------

    def _migrate_legacy(self, legacy_path):
        """mastery.db -> app.db, device_id를 user_id로 정규화하면서 딱 한 번.

        키가 세 종류로 섞여 있었다(실측 19개 키):
          users.id            그대로 쓴다
          devices.device_id   그 기기가 붙은 계정으로 접는다. 이미 그 계정에 같은 개념이
                              있으면 merge_learner와 같은 규칙 — 점수가 높은 쪽을 남긴다
                              (계정을 이어받았다고 아는 개념이 지워지면 안 된다)
          둘 다 아님           그 id로 users 행을 만든다. 귀속처를 모른다고 학습 기록을
                              말없이 버리는 것보다 낫다. 몇 건인지 반환값으로 보고한다
        """
        import os

        import migrations
        if not legacy_path or not os.path.exists(legacy_path):
            return None   # legacy_path=None -> 이관 안 함 (테스트가 진짜 mastery.db를 안 빨아들이게)
        # "대상이 비었으면 아직 안 함"은 틀린 판정이었다 — 사용자가 진척을 지우면
        # 다음 기동에 옛 mastery.db가 통째로 되살아난다(실측 확인). 원장으로 판정한다.
        already = self.db.execute("SELECT 1 FROM mastery LIMIT 1").fetchone() is not None
        if not migrations.claim(self.db, "mastery_db_to_app_db", already_done=already):
            return None
        src = sqlite3.connect(legacy_path)
        src.row_factory = sqlite3.Row
        try:
            if not src.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='mastery'").fetchone():
                return None
            dev2user = {r["device_id"]: r["user_id"]
                        for r in self.db.execute("SELECT device_id, user_id FROM devices")}
            known_users = {r["id"] for r in self.db.execute("SELECT id FROM users")}
            stats = {"rows": 0, "folded": 0, "orphan_users": 0, "collisions": 0}

            def resolve(key):
                if key in known_users:
                    return key
                if key in dev2user:
                    stats["folded"] += 1
                    return dev2user[key]
                self.db.execute(                          # 귀속처 불명 -> 계정을 만들어 보존
                    "INSERT OR IGNORE INTO users(id, display_name, created_at) VALUES(?,?,?)",
                    (key, "", time.time()))
                known_users.add(key)
                stats["orphan_users"] += 1
                return key

            for r in src.execute("SELECT * FROM mastery"):
                uid = resolve(r["device_id"])
                cur = self.db.execute(
                    "SELECT score FROM mastery WHERE user_id=? AND concept_id=?",
                    (uid, r["concept_id"])).fetchone()
                if cur is not None:
                    stats["collisions"] += 1
                    if cur["score"] >= r["score"]:
                        continue                          # 강한 쪽을 남긴다
                self.db.execute(
                    """INSERT INTO mastery(user_id,concept_id,score,ease,interval,reps,last_reviewed)
                       VALUES(?,?,?,?,?,?,?)
                       ON CONFLICT(user_id,concept_id) DO UPDATE SET
                         score=excluded.score, ease=excluded.ease, interval=excluded.interval,
                         reps=excluded.reps, last_reviewed=excluded.last_reviewed""",
                    (uid, r["concept_id"], r["score"], r["ease"], r["interval"],
                     r["reps"], r["last_reviewed"]))
                stats["rows"] += 1
            for r in src.execute("SELECT * FROM notes"):
                self.db.execute(
                    """INSERT OR IGNORE INTO notes(id,user_id,text,source,concept_id,created_at)
                       VALUES(?,?,?,?,?,?)""",
                    (r["id"], resolve(r["device_id"]), r["text"], r["source"],
                     r["concept_id"], r["created_at"]))
                stats["rows"] += 1
            for r in src.execute("SELECT * FROM quiz_attempts"):
                self.db.execute(
                    """INSERT OR IGNORE INTO quiz_attempts(id,user_id,concept_id,correct,created_at)
                       VALUES(?,?,?,?,?)""",
                    (r["id"], resolve(r["device_id"]), r["concept_id"],
                     r["correct"], r["created_at"]))
                stats["rows"] += 1
            self.db.commit()
            return stats
        finally:
            src.close()

    # ---- internal helpers ------------------------------------------------

    def _row(self, user_id, concept_id):
        return self.db.execute(
            "SELECT * FROM mastery WHERE user_id=? AND concept_id=?",
            (user_id, concept_id),
        ).fetchone()

    def _upsert(self, user_id, concept_id, score, ease, interval, reps, now):
        self.db.execute(
            """INSERT INTO mastery(user_id,concept_id,score,ease,interval,reps,last_reviewed)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(user_id,concept_id) DO UPDATE SET
                 score=excluded.score, ease=excluded.ease, interval=excluded.interval,
                 reps=excluded.reps, last_reviewed=excluded.last_reviewed""",
            (user_id, concept_id, score, ease, interval, reps, now),
        )

    # ---- spaced repetition ----------------------------------------------

    def record_quiz(self, user_id, results, now=None):
        """results: [{"concept_id": str, "correct": bool}, ...]"""
        now = time.time() if now is None else now
        for r in results:
            cid = r["concept_id"]
            self.db.execute(
                "INSERT INTO quiz_attempts(id,user_id,concept_id,correct,created_at) VALUES(?,?,?,?,?)",
                (uuid.uuid4().hex[:12], user_id, cid, 1 if r["correct"] else 0, now),
            )
            row = self._row(user_id, cid)
            score = row["score"] if row else 0.0
            ease = row["ease"] if row else START_EASE
            interval = row["interval"] if row else BASE_INTERVAL
            reps = row["reps"] if row else 0
            if r["correct"]:
                score = min(1.0, score + 0.2)          # ponytail: 1.0 ceiling
                ease = ease + 0.1
                interval = BASE_INTERVAL if reps == 0 else interval * ease
            else:
                score = max(0.0, score - 0.3)           # ponytail: 0.0 floor
                ease = max(MIN_EASE, ease - 0.2)
                interval = BASE_INTERVAL                 # reset -> resurfaces soon
            self._upsert(user_id, cid, score, ease, interval, reps + 1, now)
        self.db.commit()

    def record_covered(self, user_id, concept_ids, now=None):
        """Small exposure bump for concepts merely seen in chat (< a quiz-correct)."""
        now = time.time() if now is None else now
        for cid in concept_ids:
            row = self._row(user_id, cid)
            score = row["score"] if row else 0.0
            ease = row["ease"] if row else START_EASE
            interval = row["interval"] if row else BASE_INTERVAL
            reps = row["reps"] if row else 0
            score = min(EXPOSURE_CAP, score + 0.05)      # stays "learning", never "known"
            self._upsert(user_id, cid, score, ease, interval, reps, now)
        self.db.commit()

    def mark_known(self, user_id, concept_id, known=True, now=None):
        """User self-attests understanding of a concept. known -> score 1.0 (KNOWN),
        uncheck -> drop to learning (0.5). Resets the review clock so it isn't instantly due."""
        now = time.time() if now is None else now
        row = self._row(user_id, concept_id)
        ease = row["ease"] if row else START_EASE
        reps = row["reps"] if row else 0
        score = 1.0 if known else 0.5           # below KNOWN=0.7 -> back to "learning"
        interval = max(BASE_INTERVAL, row["interval"] if row else 0.0)
        self._upsert(user_id, concept_id, score, ease, interval, reps, now)
        self.db.commit()
        return {"concept_id": concept_id, "level": "known" if known else "learning"}

    def get_mastery(self, user_id, now=None):
        now = time.time() if now is None else now
        out = {}
        for row in self.db.execute(
            "SELECT * FROM mastery WHERE user_id=?", (user_id,)
        ):
            score = row["score"]
            level = "known" if score >= KNOWN else "learning"
            due = (row["last_reviewed"] + row["interval"]) <= now
            out[row["concept_id"]] = {
                "score": score, "level": level, "due": due, "reps": row["reps"],
            }
        return out

    def due_concepts(self, user_id, all_concept_ids, now=None):
        """Ordered review queue: overdue-and-weak first, then new, then not-due."""
        now = time.time() if now is None else now
        rows = {
            r["concept_id"]: r
            for r in self.db.execute(
                "SELECT * FROM mastery WHERE user_id=?", (user_id,)
            )
        }
        overdue_weak, new, rest = [], [], []
        for cid in all_concept_ids:
            row = rows.get(cid)
            if row is None:
                new.append(cid)
                continue
            due = (row["last_reviewed"] + row["interval"]) <= now
            if due and row["score"] < KNOWN:
                # sort key: most overdue first, then id for determinism
                overdue_weak.append((row["last_reviewed"] + row["interval"], cid))
            else:
                rest.append(cid)
        overdue_weak.sort(key=lambda t: (t[0], t[1]))
        return [c for _, c in overdue_weak] + sorted(new) + sorted(rest)

    def next_up(self, user_id, nodes, edges, now=None, limit=5):
        """Guided study path. Foundational unmastered concepts first, then concepts
        whose prerequisites are already known. Deterministic."""
        now = time.time() if now is None else now
        labels = {n["id"]: n.get("label", n["id"]) for n in nodes}
        prereqs = {n["id"]: set() for n in nodes}
        for e in edges:
            if e["relation"] in PREREQ_RELATIONS and e["source"] in prereqs:
                prereqs[e["source"]].add(e["target"])

        mastery = self.get_mastery(user_id, now=now)
        known = {c for c, m in mastery.items() if m["level"] == "known"}

        foundational, prereqs_ready = [], []
        for cid in sorted(prereqs):                      # sorted -> deterministic
            if cid in known:
                continue
            reqs = prereqs[cid]
            if not reqs:
                foundational.append((cid, "기초 개념"))
            elif reqs <= known:
                prereqs_ready.append((cid, "선수 개념 학습됨"))
        ordered = foundational + prereqs_ready
        return [
            {"concept_id": c, "label": labels.get(c, c), "reason": reason}
            for c, reason in ordered[:limit]
        ]

    # ---- dashboard (Phase 1) --------------------------------------------

    def _streak_days(self, user_id, now):
        """Consecutive-day run ending at the learner's most recent study day.

        A "study day" = any quiz_attempt or mastery.last_reviewed on that calendar
        day (UTC, day = floor(ts/86400)). Counts back from the latest active day;
        breaks on the first gap. ponytail: whole-day buckets off the epoch, no tz —
        add a tz offset only if learners complain about day boundaries.
        """
        days = {int(r[0] // 86400) for r in self.db.execute(
            "SELECT created_at FROM quiz_attempts WHERE user_id=?", (user_id,))}
        days |= {int(r[0] // 86400) for r in self.db.execute(
            "SELECT last_reviewed FROM mastery WHERE user_id=? AND last_reviewed IS NOT NULL",
            (user_id,))}
        if not days:
            return 0
        streak, d = 0, max(days)
        while d in days:
            streak += 1
            d -= 1
        return streak

    def dashboard(self, user_id, nodes, now=None):
        """The 7 Phase-1 metrics, all off data we already store. See 제안서 §02."""
        now = time.time() if now is None else now
        total_concepts = len(nodes) or 1
        mastery = self.get_mastery(user_id, now=now)
        rows = mastery.values()
        known = sum(1 for m in rows if m["level"] == "known")
        due = sum(1 for m in rows if m["due"])
        avg_score = (sum(m["score"] for m in rows) / len(rows)) if rows else 0.0

        att = self.db.execute(
            "SELECT COUNT(*) AS n, SUM(correct) AS c FROM quiz_attempts WHERE user_id=?",
            (user_id,)).fetchone()
        attempts = att["n"] or 0
        corrects = att["c"] or 0
        quiz_accuracy = (corrects / attempts) if attempts else 0.0

        streak = self._streak_days(user_id, now)
        learning_score = corrects * 10 + known * 5 + streak * 5  # 제안서: 퀴즈정답×10 + 이해완료×5 + streak보너스

        return {
            "progress_pct": round(known / total_concepts * 100, 1),
            "avg_score": round(avg_score * 100, 1),
            "quiz_accuracy": round(quiz_accuracy * 100, 1),
            "due_count": due,
            "concepts_learned": len(mastery),
            "streak_days": streak,
            "learning_score": learning_score,
            "total_concepts": len(nodes),
            "quiz_attempts": attempts,
            "quiz_correct": corrects,
        }

    # ---- cohort (Phase 3) -----------------------------------------------

    def all_learners(self):
        """Distinct learner ids with any recorded progress (for the teacher view)."""
        return [r[0] for r in self.db.execute(
            "SELECT DISTINCT user_id FROM mastery ORDER BY user_id")]

    # ---- account persistence (Phase 2) ----------------------------------

    def merge_learner(self, from_id, to_id):
        """Fold one learner's progress into another (device claims an account).

        Used when a device's anonymous progress joins a claimed account. For
        mastery rows, on a concept collision keep the STRONGER row (higher score)
        so a claim never erases known concepts. notes/quiz_attempts just reassign.
        ponytail: last-write-wins per concept via score compare; no field-level merge.
        """
        if from_id == to_id:
            return
        for row in self.db.execute("SELECT * FROM mastery WHERE user_id=?", (from_id,)).fetchall():
            dest = self._row(to_id, row["concept_id"])
            if dest is None or row["score"] > dest["score"]:
                self._upsert(to_id, row["concept_id"], row["score"], row["ease"],
                             row["interval"], row["reps"], row["last_reviewed"])
        self.db.execute("DELETE FROM mastery WHERE user_id=?", (from_id,))
        self.db.execute("UPDATE notes SET user_id=? WHERE user_id=?", (to_id, from_id))
        self.db.execute("UPDATE quiz_attempts SET user_id=? WHERE user_id=?", (to_id, from_id))
        self.db.commit()

    # ---- notes -----------------------------------------------------------

    def add_note(self, user_id, text, source="", concept_id="", now=None, conv_id=None,
                 meta=None):
        """meta는 JSON-able dict (citations 등). 답변에 붙어 있던 각주를 같이 저장한다."""
        now = time.time() if now is None else now
        note_id = uuid.uuid4().hex[:12]
        self.db.execute(
            """INSERT INTO notes(id,user_id,text,source,concept_id,created_at,conv_id,meta)
               VALUES(?,?,?,?,?,?,?,?)""",
            (note_id, user_id, text, source, concept_id, now, conv_id or None,
             json.dumps(meta, ensure_ascii=False) if meta else None),
        )
        self.db.commit()
        return note_id

    def list_notes(self, user_id):
        """노트 + 그 노트가 나온 대화의 제목.

        제목을 같이 주는 이유: 여러 대화에서 저장한 노트가 한 목록에 뒤섞이면
        "이게 어디서 나온 얘기였지"를 화면에서 알 수 없다. 지워진 대화의 노트는
        conv_id가 NULL이 되므로(ON DELETE SET NULL) 제목도 비어서 나온다.
        """
        out = []
        for r in self.db.execute(
            """SELECT n.*, c.title AS conv_title
                 FROM notes n LEFT JOIN conversations c ON c.id = n.conv_id
                WHERE n.user_id=? ORDER BY n.created_at DESC, n.id DESC""",
            (user_id,),
        ):
            d = dict(r)
            try:
                d["meta"] = json.loads(d["meta"]) if d.get("meta") else None
            except ValueError:      # 깨진 사이드카가 목록 전체를 못 쓰게 만들면 안 된다
                d["meta"] = None
            out.append(d)
        return out

    def export_markdown(self, user_id):
        notes = self.list_notes(user_id)
        if not notes:
            return "아직 저장한 노트가 없어요."
        by_concept = {}
        for n in notes:
            by_concept.setdefault(n["concept_id"] or "기타", []).append(n)
        lines = []
        for concept in sorted(by_concept):
            lines.append(f"## {concept}")
            for n in by_concept[concept]:
                src = f" — _{n['source']}_" if n["source"] else ""
                lines.append(f"> {n['text']}{src}")
                # 인용을 같이 내보낸다. 이게 없으면 내보낸 노트는 출처 없는 주장이 된다.
                for i, c in enumerate((n.get("meta") or {}).get("citations") or [], 1):
                    page = f" p.{c['start_page']}" if c.get("start_page") else ""
                    quote = (c.get("cited_text") or "").strip().replace("\n", " ")
                    lines.append(f">   [{i}] {c.get('title', '')}{page}"
                                 + (f" — “{quote[:200]}”" if quote else ""))
                if n.get("conv_title"):
                    lines.append(f">   _대화: {n['conv_title']}_")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


if __name__ == "__main__":
    import json
    import os
    import tempfile

    graph = json.load(open(os.path.join(os.path.dirname(__file__), "graph.json")))
    nodes, edges = graph["nodes"], graph["edges"]
    all_ids = [n["id"] for n in nodes]

    SELF_CHECK_USERS = ("dev1", "d", "cold", "acct", "devA", "nobody", "u", "tempuser")

    def mkuser(st, *ids):
        """진척 행은 계정에 FK로 묶인다 — 실제로도 app.py가 ACCOUNTS.resolve로 계정을
        먼저 만든 뒤에 부른다. 테스트도 같은 순서를 지켜야 현실을 모델링한다."""
        for i in (ids or SELF_CHECK_USERS):
            st.db.execute("INSERT OR IGNORE INTO users(id,display_name,created_at) VALUES(?,'',0)", (i,))
        st.db.commit()
        return st

    tmp = tempfile.mkdtemp()
    store = MasteryStore(os.path.join(tmp, "test.db"), legacy_path=None)
    dev = "dev1"
    mkuser(store, dev)

    # (0) FK: 계정이 없는 진척은 애초에 들어가지 않는다
    try:
        store.db.execute("INSERT INTO mastery(user_id,concept_id,score) VALUES('nosuch','c',0.5)")
        raise AssertionError("FK가 없다 — 존재하지 않는 계정의 진척이 저장된다")
    except sqlite3.IntegrityError:
        pass
    # 계정을 지우면 그 진척도 같이 사라진다 (CASCADE)
    mkuser(store, "tempuser")
    store.db.execute("INSERT INTO mastery(user_id,concept_id,score) VALUES('tempuser','c',0.5)")
    store.db.commit()
    with store.db:
        store.db.execute("DELETE FROM users WHERE id='tempuser'")
    assert store.db.execute("SELECT COUNT(*) FROM mastery WHERE user_id='tempuser'").fetchone()[0] == 0
    t0 = 1_000_000.0
    DAY = 86400.0

    # (1) wrong lowers, correct raises
    store.record_quiz(dev, [{"concept_id": "segmentation", "correct": True}], now=t0)
    after_correct = store.get_mastery(dev, now=t0)["segmentation"]["score"]
    store.record_quiz(dev, [{"concept_id": "segmentation", "correct": False}], now=t0)
    after_wrong = store.get_mastery(dev, now=t0)["segmentation"]["score"]
    assert after_wrong < after_correct, (after_wrong, after_correct)

    # (2) new -> learning -> known
    m = store.get_mastery(dev, now=t0)
    assert "metadata" not in m  # new: no row
    store.record_covered(dev, ["metadata"], now=t0)
    assert store.get_mastery(dev, now=t0)["metadata"]["level"] == "learning"
    for i in range(4):  # push over KNOWN via quiz corrects
        store.record_quiz(dev, [{"concept_id": "metadata", "correct": True}], now=t0)
    assert store.get_mastery(dev, now=t0)["metadata"]["level"] == "known"

    # (2b) self-attest: mark_known -> known, and not instantly due; uncheck -> learning
    #      (isolated store so it can't pollute the review-order tests below)
    ms = mkuser(MasteryStore(":memory:", legacy_path=None))
    ms.mark_known("d", "resolution", True, now=t0)
    mk = ms.get_mastery("d", now=t0)["resolution"]
    assert mk["level"] == "known" and not mk["due"], mk
    ms.mark_known("d", "resolution", False, now=t0)
    assert ms.get_mastery("d", now=t0)["resolution"]["level"] == "learning"

    # (3) long-overdue weak before fresh/new
    store.record_quiz(dev, [{"concept_id": "unet", "correct": False}], now=t0)  # weak
    later = t0 + 5 * DAY  # unet's reset interval (1 day) long past -> overdue
    order = store.due_concepts(dev, ["unet", "resolution"], now=later)  # resolution is new
    assert order.index("unet") < order.index("resolution"), order

    # (4) cold-start: foundational first; respects known prereq
    cold = mkuser(MasteryStore(":memory:", legacy_path=None))
    path = cold.next_up("cold", nodes, edges, now=t0, limit=10)
    ids = [p["concept_id"] for p in path]
    # imaging-conditions has no outgoing prereq edge -> foundational, must appear
    assert "imaging-conditions" in ids, ids
    # metadata (part_of imaging-conditions) is NOT ready until imaging-conditions known
    assert "metadata" not in ids, ids
    cold.record_quiz("cold", [{"concept_id": "imaging-conditions", "correct": True}] * 4, now=t0)
    path2 = [p["concept_id"] for p in cold.next_up("cold", nodes, edges, now=t0, limit=20)]
    assert "metadata" in path2, path2  # prereq now known -> unlocked

    # (5) note + export
    cold.add_note("cold", "U-Net은 encoder-decoder 구조다", source="Aswath 2023", concept_id="unet", now=t0)
    md = cold.export_markdown("cold")
    assert "## unet" in md and "U-Net은 encoder-decoder 구조다" in md, md
    assert mkuser(MasteryStore(":memory:", legacy_path=None)).export_markdown("nobody") == "아직 저장한 노트가 없어요."

    # (6) dashboard: metrics + streak, all off stored data (제안서 §02)
    ds = mkuser(MasteryStore(":memory:", legacy_path=None))
    DAY = 86400.0
    day0 = 100 * DAY  # a clean day boundary
    # 3 quiz attempts: 2 correct on "segmentation" (-> known after enough), 1 wrong
    ds.record_quiz("d", [{"concept_id": "segmentation", "correct": True}] * 4, now=day0)
    ds.record_quiz("d", [{"concept_id": "unet", "correct": False}], now=day0)
    d = ds.dashboard("d", nodes, now=day0)
    # 5 attempts total (4 + 1), 4 correct -> 80%
    assert d["quiz_attempts"] == 5 and d["quiz_correct"] == 4, d
    assert d["quiz_accuracy"] == 80.0, d
    assert d["concepts_learned"] == 2, d              # segmentation + unet rows
    assert d["progress_pct"] == round(1 / len(nodes) * 100, 1), d  # only segmentation known
    # learning_score = 4*10 + 1known*5 + streak(1 day)*5 = 50
    assert d["streak_days"] == 1, d
    assert d["learning_score"] == 4 * 10 + 1 * 5 + 1 * 5, d
    # streak grows on consecutive days, breaks on a gap
    ds.record_quiz("d", [{"concept_id": "unet", "correct": True}], now=day0 + DAY)
    assert ds.dashboard("d", nodes, now=day0 + DAY)["streak_days"] == 2, "consecutive day"
    ds.record_quiz("d", [{"concept_id": "unet", "correct": True}], now=day0 + 3 * DAY)  # skip a day
    assert ds.dashboard("d", nodes, now=day0 + 3 * DAY)["streak_days"] == 1, "gap resets"
    # empty device -> all zeros, no crash
    z = mkuser(MasteryStore(":memory:", legacy_path=None)).dashboard("nobody", nodes, now=day0)
    assert z["learning_score"] == 0 and z["quiz_accuracy"] == 0.0 and z["streak_days"] == 0, z

    # (7) merge_learner: device claims an account -> progress follows, keep stronger row
    mg = mkuser(MasteryStore(":memory:", legacy_path=None))
    mg.mark_known("devA", "unet", True, now=day0)                 # devA: unet known
    mg.record_quiz("devA", [{"concept_id": "resolution", "correct": True}], now=day0)
    mg.add_note("devA", "n", concept_id="unet", now=day0)
    mg.mark_known("acct", "unet", False, now=day0)               # acct: unet only learning
    mg.merge_learner("devA", "acct")
    macct = mg.get_mastery("acct", now=day0)
    assert macct["unet"]["level"] == "known", macct               # stronger row won
    assert "resolution" in macct, macct                           # devA's other progress moved
    assert mg.get_mastery("devA", now=day0) == {}, "source cleared"
    assert len(mg.list_notes("acct")) == 1, "notes reassigned"

    # (8) 노트는 어느 대화에서 나왔는지를 들고 다닌다
    nb = mkuser(MasteryStore(":memory:", legacy_path=None))
    nb.db.execute("INSERT INTO conversations(id,user_id,title) VALUES('c1','d','TEM과 SEM 차이')")
    nb.db.commit()
    nb.add_note("d", "본문", concept_id="unet", now=day0, conv_id="c1")
    nb.add_note("d", "대화 밖에서 쓴 노트", now=day0)          # conv_id 없음도 허용
    got = {n["text"]: n for n in nb.list_notes("d")}   # 같은 시각이라 순서는 id로 갈린다
    assert got["대화 밖에서 쓴 노트"]["conv_id"] is None
    assert got["대화 밖에서 쓴 노트"]["conv_title"] is None
    assert got["본문"]["conv_id"] == "c1"
    assert got["본문"]["conv_title"] == "TEM과 SEM 차이", got["본문"]
    # (8b) 노트가 인용을 데리고 저장된다 — 예전엔 첫 인용의 '제목 문자열' 하나만 남았다
    cites = [{"title": "Paper A", "start_page": 3, "cited_text": "aaa", "offset": 10,
              "source_id": "src1"},
             {"title": "Paper B", "start_page": 7, "cited_text": "bbb", "offset": 25,
              "source_id": "src2"}]
    nb.add_note("d", "인용 있는 답변 본문", concept_id="unet", now=day0, conv_id="c1",
                meta={"citations": cites})
    got2 = {n["text"]: n for n in nb.list_notes("d")}["인용 있는 답변 본문"]
    assert got2["meta"]["citations"] == cites, got2["meta"]
    assert got2["meta"]["citations"][1]["start_page"] == 7      # 첫 인용만이 아니다
    md = nb.export_markdown("d")
    assert "[1] Paper A p.3" in md and "[2] Paper B p.7" in md, md   # 내보내기에도 근거가 붙는다
    assert "대화: TEM과 SEM 차이" in md, md

    # 대화를 지워도 노트는 남는다 (ON DELETE SET NULL) — 노트가 대화보다 오래 산다
    with nb.db:
        nb.db.execute("DELETE FROM conversations WHERE id='c1'")
    after = nb.list_notes("d")
    assert len(after) == 3, f"대화를 지웠다고 노트가 사라졌다: {len(after)}"
    assert all(n["conv_id"] is None for n in after), after
    assert mg.dashboard("acct", nodes, now=day0)["quiz_attempts"] == 1, "attempts reassigned"
    assert mg.all_learners() == ["acct"], mg.all_learners()  # source folded in, one learner left

    print("all mastery self-checks passed")

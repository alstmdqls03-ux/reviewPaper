"""Read-only learning analytics over the existing SQLite stores.

Reads mastery.db (mastery + notes, from mastery.py) and app.db (users, devices,
conversations, messages, from accounts.py/history.py). Opens its own connections
(check_same_thread=False) and never writes. Every method degrades to zeros/empties
if a table or db file doesn't exist yet, so a partially-migrated box still answers.

Gate a /analytics endpoint behind ADMIN_TOKEN; the numbers are cohort-wide.
"""

import sqlite3


def _connect(db_path):
    # Read-only intent, but plain connect keeps it simple and works on a missing
    # file (creates an empty db -> queries just find no tables -> we return 0s).
    db = sqlite3.connect(db_path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    return db


def _has_table(db, name):
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _scalar(db, table, sql, default=0):
    # Run an aggregate only if the table exists; swallow shape drift to `default`.
    if not _has_table(db, table):
        return default
    try:
        row = db.execute(sql).fetchone()
        return row[0] if row and row[0] is not None else default
    except sqlite3.Error:
        return default


class Analytics:
    KNOWN = 0.7  # mirrors mastery.KNOWN; a device counts as "known" at score >= this

    def __init__(self, app_db="app.db", mastery_db=None):
        """진척과 계정이 이제 같은 파일에 있다 (2026-07-28 병합).

        _m / _a 두 핸들은 남겨뒀다 — 쿼리를 전부 다시 쓰는 대신 같은 연결을 가리키게
        했다. mastery_db 인자는 옛 호출부(테스트)를 위한 것이고, 주면 그 파일을 본다.
        """
        self._a = _connect(app_db)
        self._m = _connect(mastery_db) if mastery_db else self._a

    # ---- cohort concept mastery -----------------------------------------

    def cohort_concepts(self):
        """Per-concept aggregate across ALL devices in the mastery table."""
        if not _has_table(self._m, "mastery"):
            return []
        rows = self._m.execute(
            """
            SELECT concept_id,
                   AVG(score)                              AS avg_score,
                   COUNT(DISTINCT user_id)                AS learners,
                   SUM(CASE WHEN score >= ? THEN 1 ELSE 0 END) AS known_count
            FROM mastery
            GROUP BY concept_id
            ORDER BY learners DESC, concept_id
            """,
            (self.KNOWN,),
        ).fetchall()
        return [
            {
                "concept_id": r["concept_id"],
                "avg_score": round(r["avg_score"], 4) if r["avg_score"] is not None else 0.0,
                "learners": r["learners"],
                "known_count": r["known_count"],
            }
            for r in rows
        ]

    # ---- quiz proxy ------------------------------------------------------

    def quiz_stats(self):
        """Review volume proxied off mastery.reps / score.

        ponytail: a dedicated quiz_attempts table (one row per graded answer) would
        give true per-attempt stats. This reads reps/score off the mastery rows we
        already have -- good enough for a POC dashboard, upgrade when quizzes ship.
        """
        if not _has_table(self._m, "mastery"):
            return {"total_reviews": 0, "by_concept": []}
        total = _scalar(self._m, "mastery", "SELECT SUM(reps) FROM mastery")
        rows = self._m.execute(
            """
            SELECT concept_id,
                   SUM(reps) AS reviews,
                   AVG(score) AS avg_score
            FROM mastery
            GROUP BY concept_id
            ORDER BY reviews DESC, concept_id
            """
        ).fetchall()
        return {
            "total_reviews": int(total or 0),
            "by_concept": [
                {
                    "concept_id": r["concept_id"],
                    "reviews": int(r["reviews"] or 0),
                    "avg_score": round(r["avg_score"], 4) if r["avg_score"] is not None else 0.0,
                }
                for r in rows
            ],
        }

    # ---- engagement counts ----------------------------------------------

    def engagement(self):
        """Top-line counts across both dbs; missing tables read as 0."""
        return {
            "devices": _scalar(self._m, "mastery", "SELECT COUNT(DISTINCT user_id) FROM mastery"),
            "users": _scalar(self._a, "users", "SELECT COUNT(*) FROM users"),
            "conversations": _scalar(
                self._a, "conversations",
                "SELECT COUNT(*) FROM conversations WHERE COALESCE(deleted,0)=0",
            ),
            "messages": _scalar(self._a, "messages", "SELECT COUNT(*) FROM messages"),
            "notes": _scalar(self._m, "notes", "SELECT COUNT(*) FROM notes"),
        }

    # ---- combined --------------------------------------------------------

    def summary(self):
        return {
            "engagement": self.engagement(),
            "cohort_concepts": self.cohort_concepts(),
            "quiz_stats": self.quiz_stats(),
        }


if __name__ == "__main__":
    # Self-check on TEMP dbs. Never touches mastery.db / app.db.
    import os
    import tempfile

    d = tempfile.mkdtemp()
    mdb, adb = os.path.join(d, "mastery.db"), os.path.join(d, "app.db")

    m = sqlite3.connect(mdb)
    m.executescript(
        """
        CREATE TABLE mastery(user_id TEXT, concept_id TEXT, score REAL, ease REAL,
            interval REAL, reps INTEGER, last_reviewed REAL,
            PRIMARY KEY(user_id, concept_id));
        CREATE TABLE notes(id TEXT PRIMARY KEY, user_id TEXT, text TEXT,
            source TEXT, concept_id TEXT, created_at REAL);
        """
    )
    m.executemany(
        "INSERT INTO mastery VALUES(?,?,?,?,?,?,?)",
        [
            ("dev1", "c_bse", 0.8, 2.3, 86400, 3, 0.0),   # known
            ("dev2", "c_bse", 0.6, 2.3, 86400, 2, 0.0),   # learning
            ("dev1", "c_sem", 0.9, 2.3, 86400, 5, 0.0),   # known
        ],
    )
    m.execute("INSERT INTO notes VALUES('n1','dev1','hi','c_bse','c_bse',0.0)")
    m.commit()
    m.close()

    a = sqlite3.connect(adb)
    a.executescript(
        """
        CREATE TABLE users(id TEXT PRIMARY KEY, display_name TEXT, created_at REAL);
        CREATE TABLE devices(device_id TEXT PRIMARY KEY, user_id TEXT, linked_at REAL);
        CREATE TABLE conversations(id TEXT PRIMARY KEY, user_id TEXT, title TEXT,
            created_at REAL, updated_at REAL, deleted INTEGER DEFAULT 0);
        CREATE TABLE messages(id TEXT PRIMARY KEY, conv_id TEXT, role TEXT,
            content TEXT, created_at REAL);
        """
    )
    a.executemany("INSERT INTO users VALUES(?,?,?)", [("u1", "", 0.0), ("u2", "", 0.0)])
    a.execute("INSERT INTO conversations VALUES('cv1','u1','t',0,0,0)")
    a.execute("INSERT INTO conversations VALUES('cv2','u1','t',0,0,1)")  # deleted
    a.executemany(
        "INSERT INTO messages VALUES(?,?,?,?,?)",
        [("mA", "cv1", "user", "q", 0.0), ("mB", "cv1", "assistant", "a", 0.0)],
    )
    a.commit()
    a.close()

    an = Analytics(mastery_db=mdb, app_db=adb)

    cc = an.cohort_concepts()
    bse = next(c for c in cc if c["concept_id"] == "c_bse")
    assert bse["learners"] == 2, bse
    assert bse["known_count"] == 1, bse                       # only dev1 >= 0.7
    assert abs(bse["avg_score"] - 0.7) < 1e-9, bse            # (0.8+0.6)/2
    assert cc[0]["concept_id"] == "c_bse", cc                 # sorted by learners desc

    eng = an.engagement()
    assert eng == {"devices": 2, "users": 2, "conversations": 1, "messages": 2, "notes": 1}, eng

    qs = an.quiz_stats()
    assert qs["total_reviews"] == 10, qs                      # 3+2+5

    # Missing dbs degrade to zeros, not crashes.
    empty = Analytics(mastery_db=os.path.join(d, "nope1.db"), app_db=os.path.join(d, "nope2.db"))
    assert empty.engagement() == {"devices": 0, "users": 0, "conversations": 0, "messages": 0, "notes": 0}
    assert empty.cohort_concepts() == []

    print("analytics.py self-check OK")

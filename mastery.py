"""Per-device concept mastery, spaced repetition (SM-2-lite), guided study path, notes+export.

Stdlib only (sqlite3). All time-dependent methods take an injectable `now` (float epoch)
so they're testable without touching the wall clock.

Prerequisite model (from graph.json): an edge `{source, target, relation}` with
relation in {part_of, builds_on, uses} means `source` depends on `target` as a foundation.
So a concept's prerequisites are its outgoing targets via those relations; a concept with
no such outgoing edges is foundational (good cold-start entry point).
"""

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


class MasteryStore:
    def __init__(self, db_path="mastery.db"):
        # ponytail: single shared connection so an in-memory (":memory:") db survives
        # across calls in tests. check_same_thread=False lets the async endpoints (all on
        # the single event-loop thread) reuse it; add a lock only for threaded workers.
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS mastery(
                device_id TEXT, concept_id TEXT,
                score REAL, ease REAL, interval REAL, reps INTEGER,
                last_reviewed REAL,
                PRIMARY KEY(device_id, concept_id)
            );
            CREATE TABLE IF NOT EXISTS notes(
                id TEXT PRIMARY KEY, device_id TEXT, text TEXT,
                source TEXT, concept_id TEXT, created_at REAL
            );
            CREATE TABLE IF NOT EXISTS quiz_attempts(
                id TEXT PRIMARY KEY, device_id TEXT, concept_id TEXT,
                correct INTEGER, created_at REAL
            );
            -- 인덱스는 반드시 테이블 뒤에. executescript는 순서대로 실행하므로
            -- 앞에 두면 빈 DB에서 "no such table"로 죽는다.
            -- mastery는 PK(device_id, concept_id)의 왼쪽 접두사가 WHERE device_id=? 를 이미 탄다.
            -- notes/quiz_attempts는 PK가 id라 학습자별 조회가 풀스캔이었다 —
            -- dashboard() 한 번이 quiz_attempts를 두 번 훑는다(정답률 + streak).
            CREATE INDEX IF NOT EXISTS idx_notes_device ON notes(device_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_quiz_device ON quiz_attempts(device_id, created_at);
            """
        )
        self.db.commit()

    # ---- internal helpers ------------------------------------------------

    def _row(self, device_id, concept_id):
        return self.db.execute(
            "SELECT * FROM mastery WHERE device_id=? AND concept_id=?",
            (device_id, concept_id),
        ).fetchone()

    def _upsert(self, device_id, concept_id, score, ease, interval, reps, now):
        self.db.execute(
            """INSERT INTO mastery(device_id,concept_id,score,ease,interval,reps,last_reviewed)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(device_id,concept_id) DO UPDATE SET
                 score=excluded.score, ease=excluded.ease, interval=excluded.interval,
                 reps=excluded.reps, last_reviewed=excluded.last_reviewed""",
            (device_id, concept_id, score, ease, interval, reps, now),
        )

    # ---- spaced repetition ----------------------------------------------

    def record_quiz(self, device_id, results, now=None):
        """results: [{"concept_id": str, "correct": bool}, ...]"""
        now = time.time() if now is None else now
        for r in results:
            cid = r["concept_id"]
            self.db.execute(
                "INSERT INTO quiz_attempts(id,device_id,concept_id,correct,created_at) VALUES(?,?,?,?,?)",
                (uuid.uuid4().hex[:12], device_id, cid, 1 if r["correct"] else 0, now),
            )
            row = self._row(device_id, cid)
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
            self._upsert(device_id, cid, score, ease, interval, reps + 1, now)
        self.db.commit()

    def record_covered(self, device_id, concept_ids, now=None):
        """Small exposure bump for concepts merely seen in chat (< a quiz-correct)."""
        now = time.time() if now is None else now
        for cid in concept_ids:
            row = self._row(device_id, cid)
            score = row["score"] if row else 0.0
            ease = row["ease"] if row else START_EASE
            interval = row["interval"] if row else BASE_INTERVAL
            reps = row["reps"] if row else 0
            score = min(EXPOSURE_CAP, score + 0.05)      # stays "learning", never "known"
            self._upsert(device_id, cid, score, ease, interval, reps, now)
        self.db.commit()

    def mark_known(self, device_id, concept_id, known=True, now=None):
        """User self-attests understanding of a concept. known -> score 1.0 (KNOWN),
        uncheck -> drop to learning (0.5). Resets the review clock so it isn't instantly due."""
        now = time.time() if now is None else now
        row = self._row(device_id, concept_id)
        ease = row["ease"] if row else START_EASE
        reps = row["reps"] if row else 0
        score = 1.0 if known else 0.5           # below KNOWN=0.7 -> back to "learning"
        interval = max(BASE_INTERVAL, row["interval"] if row else 0.0)
        self._upsert(device_id, concept_id, score, ease, interval, reps, now)
        self.db.commit()
        return {"concept_id": concept_id, "level": "known" if known else "learning"}

    def get_mastery(self, device_id, now=None):
        now = time.time() if now is None else now
        out = {}
        for row in self.db.execute(
            "SELECT * FROM mastery WHERE device_id=?", (device_id,)
        ):
            score = row["score"]
            level = "known" if score >= KNOWN else "learning"
            due = (row["last_reviewed"] + row["interval"]) <= now
            out[row["concept_id"]] = {
                "score": score, "level": level, "due": due, "reps": row["reps"],
            }
        return out

    def due_concepts(self, device_id, all_concept_ids, now=None):
        """Ordered review queue: overdue-and-weak first, then new, then not-due."""
        now = time.time() if now is None else now
        rows = {
            r["concept_id"]: r
            for r in self.db.execute(
                "SELECT * FROM mastery WHERE device_id=?", (device_id,)
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

    def next_up(self, device_id, nodes, edges, now=None, limit=5):
        """Guided study path. Foundational unmastered concepts first, then concepts
        whose prerequisites are already known. Deterministic."""
        now = time.time() if now is None else now
        labels = {n["id"]: n.get("label", n["id"]) for n in nodes}
        prereqs = {n["id"]: set() for n in nodes}
        for e in edges:
            if e["relation"] in PREREQ_RELATIONS and e["source"] in prereqs:
                prereqs[e["source"]].add(e["target"])

        mastery = self.get_mastery(device_id, now=now)
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

    def _streak_days(self, device_id, now):
        """Consecutive-day run ending at the learner's most recent study day.

        A "study day" = any quiz_attempt or mastery.last_reviewed on that calendar
        day (UTC, day = floor(ts/86400)). Counts back from the latest active day;
        breaks on the first gap. ponytail: whole-day buckets off the epoch, no tz —
        add a tz offset only if learners complain about day boundaries.
        """
        days = {int(r[0] // 86400) for r in self.db.execute(
            "SELECT created_at FROM quiz_attempts WHERE device_id=?", (device_id,))}
        days |= {int(r[0] // 86400) for r in self.db.execute(
            "SELECT last_reviewed FROM mastery WHERE device_id=? AND last_reviewed IS NOT NULL",
            (device_id,))}
        if not days:
            return 0
        streak, d = 0, max(days)
        while d in days:
            streak += 1
            d -= 1
        return streak

    def dashboard(self, device_id, nodes, now=None):
        """The 7 Phase-1 metrics, all off data we already store. See 제안서 §02."""
        now = time.time() if now is None else now
        total_concepts = len(nodes) or 1
        mastery = self.get_mastery(device_id, now=now)
        rows = mastery.values()
        known = sum(1 for m in rows if m["level"] == "known")
        due = sum(1 for m in rows if m["due"])
        avg_score = (sum(m["score"] for m in rows) / len(rows)) if rows else 0.0

        att = self.db.execute(
            "SELECT COUNT(*) AS n, SUM(correct) AS c FROM quiz_attempts WHERE device_id=?",
            (device_id,)).fetchone()
        attempts = att["n"] or 0
        corrects = att["c"] or 0
        quiz_accuracy = (corrects / attempts) if attempts else 0.0

        streak = self._streak_days(device_id, now)
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
            "SELECT DISTINCT device_id FROM mastery ORDER BY device_id")]

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
        for row in self.db.execute("SELECT * FROM mastery WHERE device_id=?", (from_id,)).fetchall():
            dest = self._row(to_id, row["concept_id"])
            if dest is None or row["score"] > dest["score"]:
                self._upsert(to_id, row["concept_id"], row["score"], row["ease"],
                             row["interval"], row["reps"], row["last_reviewed"])
        self.db.execute("DELETE FROM mastery WHERE device_id=?", (from_id,))
        self.db.execute("UPDATE notes SET device_id=? WHERE device_id=?", (to_id, from_id))
        self.db.execute("UPDATE quiz_attempts SET device_id=? WHERE device_id=?", (to_id, from_id))
        self.db.commit()

    # ---- notes -----------------------------------------------------------

    def add_note(self, device_id, text, source="", concept_id="", now=None):
        now = time.time() if now is None else now
        note_id = uuid.uuid4().hex[:12]
        self.db.execute(
            "INSERT INTO notes(id,device_id,text,source,concept_id,created_at) VALUES(?,?,?,?,?,?)",
            (note_id, device_id, text, source, concept_id, now),
        )
        self.db.commit()
        return note_id

    def list_notes(self, device_id):
        return [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM notes WHERE device_id=? ORDER BY created_at DESC, id DESC",
                (device_id,),
            )
        ]

    def export_markdown(self, device_id):
        notes = self.list_notes(device_id)
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
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


if __name__ == "__main__":
    import json
    import os
    import tempfile

    graph = json.load(open(os.path.join(os.path.dirname(__file__), "graph.json")))
    nodes, edges = graph["nodes"], graph["edges"]
    all_ids = [n["id"] for n in nodes]

    tmp = tempfile.mkdtemp()
    store = MasteryStore(os.path.join(tmp, "test.db"))
    dev = "dev1"
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
    ms = MasteryStore(":memory:")
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
    cold = MasteryStore(":memory:")
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
    assert MasteryStore(":memory:").export_markdown("nobody") == "아직 저장한 노트가 없어요."

    # (6) dashboard: metrics + streak, all off stored data (제안서 §02)
    ds = MasteryStore(":memory:")
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
    z = MasteryStore(":memory:").dashboard("nobody", nodes, now=day0)
    assert z["learning_score"] == 0 and z["quiz_accuracy"] == 0.0 and z["streak_days"] == 0, z

    # (7) merge_learner: device claims an account -> progress follows, keep stronger row
    mg = MasteryStore(":memory:")
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
    assert mg.dashboard("acct", nodes, now=day0)["quiz_attempts"] == 1, "attempts reassigned"
    assert mg.all_learners() == ["acct"], mg.all_learners()  # source folded in, one learner left

    print("all mastery self-checks passed")

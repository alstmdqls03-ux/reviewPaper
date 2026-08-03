"""In-process tests for session.py (no HTTP).

Verifies: (a) session isolation, (b) quiz_available threshold, (c) TTL eviction,
(d) quiz grading. Run: `pytest test_session.py -v`.

ASSUMED INTERFACE (reconcile with the real session.py — this was written before
session.py existed). If the real API differs, adapt these calls; the intent per
test is what matters:
  - SessionStore(max_age_seconds=...) with:
      .get_or_create(session_id: str | None) -> Session
      .evict_expired(now: float) -> int              # returns/removes stale
  - Session with:
      .id (str), .messages (list), .covered_concepts (set), .turns (int)
      .quiz_available -> bool  (True when len(covered_concepts) >= 3 or turns >= 4)
      .add_quiz(questions: list) -> quiz_id (str)
      .grade_quiz(quiz_id, answers: list[int]) -> {"score","total","results"}

Each quiz question is assumed to carry a "correct_index" (or "answer") key so
grading is deterministic.
"""
import time

import pytest

session = pytest.importorskip(
    "session", reason="session.py not present yet — skipping in-process tests")

SessionStore = session.SessionStore


def _new_store(max_age=3600):
    # Tolerate either constructor kwarg name.
    try:
        return SessionStore(max_age_seconds=max_age)
    except TypeError:
        return SessionStore(max_age)


def _correct_index(q):
    for k in ("answer_index", "correct_index", "answer", "correct"):
        if k in q:
            return q[k]
    raise KeyError("quiz question has no correct-answer key")


def _sample_questions():
    return [
        {"id": "q1", "question": "A?", "options": ["a", "b", "c", "d"],
         "answer_index": 0, "explanation": "a", "concept_id": "x", "source": "P"},
        {"id": "q2", "question": "B?", "options": ["a", "b", "c", "d"],
         "answer_index": 2, "explanation": "b", "concept_id": "y", "source": "P"},
    ]


def test_session_isolation():
    store = _new_store()
    s1 = store.get_or_create("alice")
    s2 = store.get_or_create("bob")

    assert s1.id != s2.id
    s1.messages.append({"role": "user", "content": "hi from alice"})
    s1.covered_concepts.add("segmentation")

    # bob must be untouched
    assert s2.messages == []
    assert "segmentation" not in s2.covered_concepts
    # same id returns same object (no duplicate state)
    assert store.get_or_create("alice") is s1


def test_quiz_available_threshold():
    store = _new_store()
    s = store.get_or_create("carol")
    assert s.quiz_available is False

    # Flip via covered_concepts >= 3
    s.covered_concepts.update({"segmentation", "metadata", "fair"})
    assert s.quiz_available is True

    # Fresh session flips via turns >= 4 instead
    s2 = store.get_or_create("dave")
    assert s2.quiz_available is False
    for _ in range(4):
        s2.turns += 1
    assert s2.quiz_available is True


def test_ttl_eviction():
    store = _new_store(max_age=10)
    old = store.get_or_create("stale")
    old_id = old.id

    # Simulate time passing well beyond max age.
    removed = store.evict_expired(now=time.time() + 10_000)
    assert removed  # something was evicted (int count or truthy)

    # A subsequent get_or_create for the same id makes a *new* empty session.
    fresh = store.get_or_create("stale")
    assert fresh.messages == []
    assert fresh.covered_concepts == set()


def test_grade_quiz():
    store = _new_store()
    s = store.get_or_create("erin")
    qs = _sample_questions()
    quiz_id = s.add_quiz(qs)

    # All correct
    graded = s.grade_quiz(quiz_id, [_correct_index(q) for q in qs])
    assert graded["total"] == len(qs)
    assert graded["score"] == len(qs)
    assert all(r["correct"] for r in graded["results"])

    # One wrong -> score drops by one
    answers = [_correct_index(qs[0]), (_correct_index(qs[1]) + 1) % 4]
    graded2 = s.grade_quiz(quiz_id, answers)
    assert graded2["score"] == len(qs) - 1

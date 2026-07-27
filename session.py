"""In-memory session store: per-session chat history, covered concepts, and quizzes.

Drives multi-turn chat, the quiz-readiness signal, and server-side quiz grading.
ponytail: in-memory single-process store guarded by the asyncio event loop (one
thread). Swap for Redis only if we ever run multiple workers or need persistence.
"""
import asyncio
import time
import uuid
from dataclasses import dataclass, field

# Quiz unlocks once the learner has actually covered ground.
QUIZ_MIN_CONCEPTS = 3
QUIZ_MIN_TURNS = 4
DEFAULT_TTL = 1800  # 30 min of inactivity


@dataclass
class Session:
    id: str
    created_at: float
    last_active: float
    messages: list = field(default_factory=list)      # [{"role","content"}] text-only history
    covered_concepts: set = field(default_factory=set)  # graph node ids touched so far
    turns: int = 0                                       # completed user<->assistant exchanges
    quizzes: dict = field(default_factory=dict)          # quiz_id -> [full question dicts]
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # serialize one session's requests

    @property
    def quiz_available(self) -> bool:
        return len(self.covered_concepts) >= QUIZ_MIN_CONCEPTS or self.turns >= QUIZ_MIN_TURNS

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_assistant(self, text: str, concepts) -> None:
        self.messages.append({"role": "assistant", "content": text})
        self.covered_concepts |= set(concepts)
        self.turns += 1

    def reset(self) -> None:
        """Wipe chat history, covered concepts, turn count, and quizzes in place.

        Keeps the same id/lock so the client can keep its session_id and just
        start over. Conversation persistence (history.py) is untouched — only
        the live in-memory state resets.
        """
        self.messages.clear()
        self.covered_concepts.clear()
        self.turns = 0
        self.quizzes.clear()

    def add_quiz(self, questions: list) -> str:
        """Store the full questions (with answers) and return a quiz_id."""
        quiz_id = uuid.uuid4().hex[:12]
        self.quizzes[quiz_id] = questions
        return quiz_id

    def grade_quiz(self, quiz_id: str, answers: list) -> dict:
        qs = self.quizzes.get(quiz_id)
        if qs is None:
            raise KeyError(quiz_id)
        results, score = [], 0
        for i, q in enumerate(qs):
            picked = answers[i] if i < len(answers) else None
            correct = picked == q["answer_index"]
            if correct:
                score += 1
            results.append({
                "id": q["id"],
                "correct": correct,
                "correct_index": q["answer_index"],
                "explanation": q["explanation"],
                "concept_id": q.get("concept_id", ""),
                "source": q.get("source", ""),
            })
        return {"score": score, "total": len(qs), "results": results}


class SessionStore:
    def __init__(self, ttl: float = DEFAULT_TTL):
        self.ttl = ttl
        self._sessions: dict[str, Session] = {}

    def get_or_create(self, session_id: str | None, now: float | None = None) -> Session:
        now = now if now is not None else time.time()
        self.evict_expired(now)
        if session_id and session_id in self._sessions:
            s = self._sessions[session_id]
            s.last_active = now
            return s
        sid = session_id or uuid.uuid4().hex  # honor a client id even if it had expired
        s = Session(id=sid, created_at=now, last_active=now)
        self._sessions[sid] = s
        return s

    def evict_expired(self, now: float | None = None) -> int:
        now = now if now is not None else time.time()
        dead = [sid for sid, s in self._sessions.items() if now - s.last_active > self.ttl]
        for sid in dead:
            del self._sessions[sid]
        return len(dead)

    def __len__(self):
        return len(self._sessions)


def match_concepts(text: str, nodes: list) -> list:
    """Map an answer to graph node ids by matching node labels/ids in the text.

    ponytail: case-insensitive substring match on label + id-as-words. Good enough
    to light up the graph; upgrade to model concept-tagging if it gets noisy.
    """
    low = text.lower()
    hits = []
    for n in nodes:
        label = n.get("label", "").lower()
        id_words = n["id"].replace("-", " ")
        # split multi-label like "SEM / TEM / STEM" into alternatives
        aliases = [a.strip() for a in label.replace("/", ",").split(",") if len(a.strip()) >= 4]
        aliases.append(id_words)
        if any(a and a in low for a in aliases):
            hits.append(n["id"])
    return hits


if __name__ == "__main__":
    # Self-check: isolation, quiz gating, grading, eviction.
    store = SessionStore(ttl=100)
    a = store.get_or_create(None, now=1000)
    b = store.get_or_create(None, now=1000)
    assert a.id != b.id
    a.add_assistant("about segmentation", ["segmentation"])
    assert b.covered_concepts == set() and a.covered_concepts == {"segmentation"}  # isolated

    assert not a.quiz_available
    a.add_assistant("x", ["metadata", "fair"])
    assert a.quiz_available  # 3 concepts

    qid = a.add_quiz([
        {"id": "q1", "answer_index": 2, "explanation": "because", "concept_id": "segmentation", "source": "P"},
        {"id": "q2", "answer_index": 0, "explanation": "yep", "concept_id": "metadata", "source": "P"},
    ])
    g = a.grade_quiz(qid, [2, 3])
    assert g["score"] == 1 and g["total"] == 2 and g["results"][0]["correct"]

    nodes = [{"id": "segmentation", "label": "Segmentation"}, {"id": "fair", "label": "FAIR principles"}]
    assert set(match_concepts("This covers Segmentation and the FAIR principles.", nodes)) == {"segmentation", "fair"}

    a.reset()  # wipe in place, same id, quiz gating back to locked
    assert a.covered_concepts == set() and a.turns == 0 and a.quizzes == {} and a.messages == []
    assert not a.quiz_available

    store.get_or_create(a.id, now=1150)  # touching a here already evicts the stale b
    assert len(store) == 1 and a.id in store._sessions and b.id not in store._sessions
    print("session.py self-check ok")

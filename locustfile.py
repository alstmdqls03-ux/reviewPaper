"""Concurrency / session-isolation load test for the review-paper chatbot.

Proves parallel users don't bleed state: each simulated user creates its OWN
session and every subsequent response must echo back THAT SAME session_id.
Any mismatch is a hard failure — that's the whole point of this test.

Run:
    MOCK_LLM=1 python app.py                                        # terminal 1
    locust -f locustfile.py --host http://127.0.0.1:8000 -u 20 -r 5 -t 60s
    # or headless:
    locust -f locustfile.py --host http://127.0.0.1:8000 \
           --headless -u 20 -r 5 -t 60s
"""
import json

from locust import HttpUser, task, between


def _parse_sse(raw):
    """Yield decoded event dicts from an SSE response body."""
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        try:
            yield json.loads(line[len("data:"):].strip())
        except json.JSONDecodeError:
            continue


class ChatUser(HttpUser):
    wait_time = between(1, 3)

    def on_start(self):
        # Each simulated user is its own device — real clients send X-Device-Id, and the
        # server rate-limits per device, so without this all users share the host key.
        import uuid
        self.device = uuid.uuid4().hex

    def _chat(self, message, expect_session=None):
        """One /chat round-trip. Returns (session_id, done_event) or (None, None).

        expect_session: if given, the session echoed back MUST equal it, else fail.
        """
        payload = {"session_id": expect_session, "message": message}
        with self.client.post("/chat", json=payload, stream=True, headers={"X-Device-Id": self.device},
                              catch_response=True, name="/chat") as resp:
            if resp.status_code != 200:
                resp.failure(f"HTTP {resp.status_code}")
                return None, None
            session_id, done = None, None
            for ev in _parse_sse(resp.text):
                t = ev.get("type")
                if t == "session":
                    session_id = ev.get("session_id")
                elif t == "done":
                    done = ev
                elif t == "error":
                    resp.failure(f"stream error: {ev.get('message')}")
                    return None, None

            if session_id is None:
                resp.failure("no session event in stream")
                return None, None
            # CORE ASSERTION: isolation — never see another user's session.
            if expect_session is not None and session_id != expect_session:
                resp.failure(
                    f"session bleed: sent {expect_session} got {session_id}")
                return None, None
            resp.success()
            return session_id, done

    @task
    def conversation(self):
        # 1) Establish our own session.
        sid, done = self._chat("What is semantic segmentation in EM?")
        if sid is None:
            return

        # 2) Follow-ups reuse OUR session_id; each response must echo it.
        follow_ups = [
            "Why is annotation a bottleneck?",
            "How does denoising help at low dose?",
            "Which imaging conditions matter most?",
        ]
        quiz_available = bool(done and done.get("quiz_available"))
        for msg in follow_ups:
            _, done = self._chat(msg, expect_session=sid)
            if done and done.get("quiz_available"):
                quiz_available = True

        # 3) Optionally pull a quiz once available — must key off OUR session.
        if quiz_available:
            with self.client.post("/quiz", json={"session_id": sid}, headers={"X-Device-Id": self.device},
                                  catch_response=True, name="/quiz") as resp:
                if resp.status_code != 200:
                    resp.failure(f"HTTP {resp.status_code}")
                    return
                body = resp.json()
                if not body.get("questions"):
                    resp.failure("quiz returned no questions")
                else:
                    resp.success()

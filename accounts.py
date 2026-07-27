"""Anonymous-device -> account mapping, optional display names, signed stateless tokens.

Stdlib only. Time-dependent methods take an injectable `now` (float epoch) for tests.
Opens the shared production db (app.db) — NOT mastery.db.
"""

import hmac
import os
import sqlite3
import time
import uuid
from hashlib import sha256

# APP_SECRET signs cross-device account tokens. MUST be set in production —
# the dev default is public and lets anyone forge any user's token.
SECRET = os.getenv("APP_SECRET", "dev-insecure-change-me").encode()


def _connect(db_path):
    # ponytail: single shared connection, single event-loop thread (see mastery.py).
    # check_same_thread=False reuses it across async endpoints. Ceiling: one thread /
    # one worker. Upgrade path: per-request connection or a small pool for multi-worker.
    db = sqlite3.connect(db_path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    return db


class Accounts:
    def __init__(self, db_path="app.db"):
        self.db = _connect(db_path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                id TEXT PRIMARY KEY, display_name TEXT, created_at REAL
            );
            CREATE TABLE IF NOT EXISTS devices(
                device_id TEXT PRIMARY KEY, user_id TEXT, linked_at REAL
            );
            """
        )
        self.db.commit()

    # ---- device <-> user -------------------------------------------------

    def resolve(self, device_id, now=None):
        """user_id for a device; create a new anonymous user if unknown. Idempotent."""
        now = time.time() if now is None else now
        row = self.db.execute(
            "SELECT user_id FROM devices WHERE device_id=?", (device_id,)
        ).fetchone()
        if row:
            return row["user_id"]
        user_id = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO users(id,display_name,created_at) VALUES(?,?,?)",
            (user_id, "", now),
        )
        self.db.execute(
            "INSERT INTO devices(device_id,user_id,linked_at) VALUES(?,?,?)",
            (device_id, user_id, now),
        )
        self.db.commit()
        return user_id

    def link_device(self, user_id, device_id, now=None):
        """Attach a device to an existing user; re-point it if it maps elsewhere."""
        now = time.time() if now is None else now
        self.db.execute(
            """INSERT INTO devices(device_id,user_id,linked_at) VALUES(?,?,?)
               ON CONFLICT(device_id) DO UPDATE SET user_id=excluded.user_id,
                 linked_at=excluded.linked_at""",
            (device_id, user_id, now),
        )
        self.db.commit()

    # ---- profile ---------------------------------------------------------

    def set_name(self, user_id, name):
        self.db.execute(
            "UPDATE users SET display_name=? WHERE id=?", (name, user_id)
        )
        self.db.commit()

    def get(self, user_id):
        row = self.db.execute(
            "SELECT * FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if row is None:
            return None
        devices = [
            r["device_id"]
            for r in self.db.execute(
                "SELECT device_id FROM devices WHERE user_id=? ORDER BY linked_at",
                (user_id,),
            )
        ]
        return {
            "id": row["id"],
            "display_name": row["display_name"],
            "created_at": row["created_at"],
            "devices": devices,
        }

    # ---- signed stateless tokens ----------------------------------------

    def issue_token(self, user_id):
        digest = hmac.new(SECRET, user_id.encode(), sha256).hexdigest()
        return f"{user_id}.{digest}"

    def verify_token(self, token):
        """Return user_id if the HMAC matches (constant-time), else None."""
        user_id, _, digest = token.rpartition(".")
        if not user_id:
            return None
        expected = hmac.new(SECRET, user_id.encode(), sha256).hexdigest()
        return user_id if hmac.compare_digest(expected, digest) else None


if __name__ == "__main__":
    import tempfile
    import os as _os

    db = _os.path.join(tempfile.mkdtemp(), "test.db")
    a = Accounts(db)
    t0 = 1_000_000.0

    # same device -> same user twice
    u1 = a.resolve("devA", now=t0)
    assert a.resolve("devA", now=t0) == u1

    # new device -> new user
    u2 = a.resolve("devB", now=t0)
    assert u2 != u1

    # link_device re-points devB to u1
    a.link_device(u1, "devB", now=t0)
    assert a.resolve("devB", now=t0) == u1

    # set_name / get round-trip
    a.set_name(u1, "민승빈")
    g = a.get(u1)
    assert g["display_name"] == "민승빈"
    assert set(g["devices"]) == {"devA", "devB"}, g["devices"]

    # token issue -> verify, tamper -> None
    tok = a.issue_token(u1)
    assert a.verify_token(tok) == u1
    assert a.verify_token(tok + "x") is None
    assert a.verify_token("someoneelse." + tok.split(".")[1]) is None

    print("all accounts self-checks passed")

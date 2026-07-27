"""Centralized settings read from the environment at import time.

Values are read once when `Settings()` is constructed. The module-level
`settings` singleton is created at import, so env changes after import are
ignored (re-instantiate `Settings()` if you need to re-read, e.g. in tests).

Secrets (APP_SECRET, ADMIN_TOKEN) are NEVER exposed by summary().
"""
import os

from dotenv import load_dotenv

# Load .env HERE, not in whichever module happens to be imported first — otherwise
# MOCK's answer depends on import order (papers.py used to be the only loader).
load_dotenv()


class Settings:
    def __init__(self):
        self.MODEL = os.getenv("MODEL", "claude-opus-4-8")
        self.SESSION_TTL = int(os.getenv("SESSION_TTL", "1800"))
        self.RATE_LIMIT = int(os.getenv("RATE_LIMIT", "30"))      # requests
        self.RATE_WINDOW = int(os.getenv("RATE_WINDOW", "60"))    # seconds
        self.MAX_MESSAGE_CHARS = int(os.getenv("MAX_MESSAGE_CHARS", "4000"))
        self.MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "50"))
        self.MAX_SOURCES = int(os.getenv("MAX_SOURCES", "50"))    # per user, own uploads only
        # Mock when explicitly asked OR when there is no key to call with. llm.MOCK reads
        # this exact value, so /healthz can't claim "mock: false" while answers are canned.
        self.MOCK = os.getenv("MOCK_LLM") == "1" or not os.getenv("ANTHROPIC_API_KEY")
        self.APP_SECRET = os.getenv("APP_SECRET", "dev-only-insecure-change-me")
        self.ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")  # empty => admin open in dev

    def summary(self) -> dict:
        """Non-secret settings, safe for /healthz and startup logs.

        Never includes APP_SECRET or ADMIN_TOKEN. Reports whether an admin
        token is configured (bool) without leaking its value.
        """
        return {
            "model": self.MODEL,
            "session_ttl": self.SESSION_TTL,
            "rate_limit": self.RATE_LIMIT,
            "rate_window": self.RATE_WINDOW,
            "max_message_chars": self.MAX_MESSAGE_CHARS,
            "max_upload_mb": self.MAX_UPLOAD_MB,
            "max_sources": self.MAX_SOURCES,
            "mock": self.MOCK,
            "admin_auth_enabled": bool(self.ADMIN_TOKEN),
        }


settings = Settings()


if __name__ == "__main__":
    # defaults load — isolate from ambient env (e.g. MOCK_LLM=1 in the shell)
    for _k in ("MODEL", "SESSION_TTL", "RATE_LIMIT", "RATE_WINDOW", "MAX_SOURCES",
               "MAX_MESSAGE_CHARS", "MAX_UPLOAD_MB", "MOCK_LLM", "ADMIN_TOKEN",
               "ANTHROPIC_API_KEY"):
        os.environ.pop(_k, None)
    s = Settings()
    assert s.MODEL == "claude-opus-4-8"
    assert s.SESSION_TTL == 1800 and s.RATE_LIMIT == 30 and s.RATE_WINDOW == 60
    assert s.MAX_MESSAGE_CHARS == 4000 and s.MAX_UPLOAD_MB == 50 and s.MAX_SOURCES == 50
    assert s.ADMIN_TOKEN == ""

    # MOCK: no key -> mock even without MOCK_LLM; a key -> real; MOCK_LLM=1 forces mock.
    assert s.MOCK is True, "keyless must be mock — otherwise /healthz lies"
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-not-a-real-key"
    assert Settings().MOCK is False
    os.environ["MOCK_LLM"] = "1"
    assert Settings().MOCK is True, "MOCK_LLM=1 must win even with a key present"
    os.environ.pop("MOCK_LLM")
    # llm.py must not carry its own copy of the rule
    import pathlib
    _llm = pathlib.Path("llm.py").read_text()
    assert 'MOCK = settings.MOCK' in _llm, "llm.py re-derives MOCK — the two will drift"

    # env override works on re-instantiation
    os.environ["RATE_LIMIT"] = "5"
    os.environ["MOCK_LLM"] = "1"
    os.environ["ADMIN_TOKEN"] = "secret-token"
    s2 = Settings()
    assert s2.RATE_LIMIT == 5
    assert s2.MOCK is True
    assert s2.ADMIN_TOKEN == "secret-token"

    # summary excludes secrets
    summ = s2.summary()
    assert "APP_SECRET" not in summ and "ADMIN_TOKEN" not in summ
    assert "app_secret" not in summ and "admin_token" not in summ
    assert "secret-token" not in summ.values()
    assert summ["admin_auth_enabled"] is True
    assert summ["rate_limit"] == 5 and summ["mock"] is True

    print("config.py self-check OK:", summ)

"""Thread-safe token-bucket rate limiter + input validators. Stdlib only.

# ponytail: in-memory per-process buckets. Correct for a single worker, but
# each uvicorn/gunicorn worker keeps its own buckets, so the effective limit
# is RATE_LIMIT * num_workers. Move to a shared store (Redis INCR/EXPIRE or a
# Lua token bucket) when you run more than one worker.
"""
import threading
import time


class RateLimiter:
    """Token bucket per key.

    Burst cap = `rate` tokens; refills at `rate / per_seconds` tokens/sec.
    Each allowed request consumes 1 token. `now` is injectable everywhere so
    the math is deterministic in tests; defaults to time.monotonic().
    """

    def __init__(self, rate: int, per_seconds: float):
        assert rate > 0 and per_seconds > 0
        self.rate = float(rate)
        self.per_seconds = float(per_seconds)
        self.refill_per_sec = self.rate / self.per_seconds
        self._lock = threading.Lock()
        # key -> [tokens, last_ts]
        self._buckets = {}
        self._last_cleanup = 0.0

    def _now(self, now):
        return time.monotonic() if now is None else now

    def _refill_locked(self, key, now):
        """Return current token count for key after refilling. Caller holds lock."""
        tokens, last = self._buckets.get(key, (self.rate, now))
        tokens = min(self.rate, tokens + (now - last) * self.refill_per_sec)
        self._buckets[key] = (tokens, now)
        return tokens

    def allow(self, key, now=None) -> bool:
        now = self._now(now)
        with self._lock:
            tokens = self._refill_locked(key, now)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                allowed = True
            else:
                allowed = False
        # opportunistic cleanup, at most once per window, outside the hot path
        if now - self._last_cleanup >= self.per_seconds:
            self.cleanup(now)
        return allowed

    def retry_after(self, key, now=None) -> float:
        """Seconds until the next token is available (0.0 if one is ready)."""
        now = self._now(now)
        with self._lock:
            tokens, last = self._buckets.get(key, (self.rate, now))
            tokens = min(self.rate, tokens + (now - last) * self.refill_per_sec)
            if tokens >= 1.0:
                return 0.0
            return (1.0 - tokens) / self.refill_per_sec

    def cleanup(self, now=None):
        """Drop buckets that have refilled back to full (idle keys)."""
        now = self._now(now)
        with self._lock:
            self._last_cleanup = now
            for key in list(self._buckets):
                tokens, last = self._buckets[key]
                if min(self.rate, tokens + (now - last) * self.refill_per_sec) >= self.rate:
                    del self._buckets[key]


def check_message(text, max_chars: int) -> str:
    """Validate a chat message. Returns the stripped text, or raises ValueError."""
    if text is None or not str(text).strip():
        raise ValueError("메시지를 입력해 주세요.")
    text = str(text)
    if len(text) > max_chars:
        raise ValueError(f"메시지가 너무 깁니다. 최대 {max_chars}자까지 입력할 수 있습니다.")
    return text


def check_upload_size(num_bytes: int, max_mb: int) -> None:
    """Raise ValueError if the upload exceeds max_mb megabytes."""
    limit = max_mb * 1024 * 1024
    if num_bytes > limit:
        raise ValueError(f"파일이 너무 큽니다. 최대 {max_mb}MB까지 업로드할 수 있습니다.")


if __name__ == "__main__":
    # rate=3 per 60s, all at the same instant t=0
    rl = RateLimiter(rate=3, per_seconds=60)
    assert rl.allow("a", now=0) is True
    assert rl.allow("a", now=0) is True
    assert rl.allow("a", now=0) is True
    assert rl.allow("a", now=0) is False          # 4th blocked
    assert rl.retry_after("a", now=0) > 0          # blocked -> wait > 0

    # one token refills after per_seconds/rate = 20s
    assert rl.retry_after("a", now=0) == 20.0
    assert rl.allow("a", now=20) is True           # exactly one token back

    # full window later -> fully refilled, allowed again, retry_after ~0
    assert rl.allow("a", now=200) is True
    assert rl.retry_after("b", now=0) == 0.0       # fresh key, ready

    # separate keys are independent
    assert rl.allow("other", now=0) is True

    # cleanup drops idle (fully refilled) buckets
    rl.cleanup(now=10_000)
    assert rl._buckets == {}

    # validators
    assert check_message("  hi  ", 4000) == "  hi  "
    for bad in ("", "   ", None):
        try:
            check_message(bad, 4000)
            assert False, "empty should raise"
        except ValueError:
            pass
    try:
        check_message("x" * 4001, 4000)
        assert False, "over-long should raise"
    except ValueError:
        pass

    check_upload_size(50 * 1024 * 1024, 50)        # exactly at limit: ok
    try:
        check_upload_size(50 * 1024 * 1024 + 1, 50)
        assert False, "oversize should raise"
    except ValueError:
        pass

    print("limits.py self-check OK")

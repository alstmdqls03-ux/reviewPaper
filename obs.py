"""Observability helpers: structured logging, per-request metrics, cost estimate.

Framework-light and import-clean so app.py can wire it in. Public API:
    log_line(**fields)                              -> print one JSON line to stdout
    estimate_cost(input_tokens, output_tokens, model=...) -> USD float
    Metrics().record(**kw)                          -> accumulate a request record
    Metrics().summary()                             -> {count, avg_latency_ms,
                                                        p95_latency_ms, by_path,
                                                        total_est_cost_usd}

Wiring as ASGI middleware (do this in app.py; obs.py stays framework-free):

    from obs import Metrics, log_line, estimate_cost
    import time, uuid
    METRICS = Metrics()

    @app.middleware("http")
    async def observe(request, call_next):
        rid = uuid.uuid4().hex[:8]
        t0 = time.perf_counter()
        response = await call_next(request)
        latency_ms = (time.perf_counter() - t0) * 1000
        METRICS.record(request_id=rid, path=request.url.path,
                       session_id=request.headers.get("x-session-id"),
                       latency_ms=latency_ms, status=response.status_code,
                       est_cost_usd=0.0)  # set real cost where token counts are known
        log_line(request_id=rid, path=request.url.path, status=response.status_code,
                 latency_ms=round(latency_ms, 1))
        return response

    # expose the rollup: @app.get("/metrics") -> METRICS.summary()

Note: streaming responses (SSE /chat) finish emitting after the middleware
returns, so latency here is time-to-first-byte, not full stream duration; and
token counts aren't known at middleware level — record est_cost from inside the
handler where usage is available. ponytail: TTFB is the honest cheap metric.
"""
import json
import sys
import time

# $ per 1M tokens (input/output/cache-read). Extend as models are added.
# cache_read ≈ 0.1× input (prompt caching); we already cache the last doc block.
_RATES = {
    "claude-opus-4-8": {"in": 5.0, "out": 25.0},
    "claude-sonnet-5": {"in": 3.0, "out": 15.0},
    "claude-haiku-4-5": {"in": 1.0, "out": 5.0},
}


def estimate_cost(input_tokens: int, output_tokens: int, model: str = "claude-opus-4-8",
                  cache_read_tokens: int = 0) -> float:
    r = _RATES.get(model, _RATES["claude-opus-4-8"])
    cache = (cache_read_tokens / 1_000_000) * r["in"] * 0.1  # cached reads bill ~10%
    return (input_tokens / 1_000_000) * r["in"] + (output_tokens / 1_000_000) * r["out"] + cache


def log_line(**fields) -> None:
    """Emit one JSON line (structured log) to stdout."""
    fields.setdefault("ts", time.time())
    print(json.dumps(fields, ensure_ascii=False, default=str), file=sys.stdout, flush=True)


def _percentile(sorted_vals, p):
    """Nearest-rank percentile on an already-sorted list. p in [0,1]."""
    if not sorted_vals:
        return 0.0
    import math
    k = max(0, math.ceil(p * len(sorted_vals)) - 1)
    return sorted_vals[k]


class Metrics:
    """In-memory accumulator. ponytail: single-process, no lock — wrap with one
    if you ever run threaded workers that share an instance."""

    def __init__(self):
        self._records = []
        self._extra_cost = {}  # path -> USD, for costs known only after streaming (token usage)

    def add_cost(self, path: str, usd: float):
        """Attribute a token-based cost to a path, out-of-band from record().
        Used by /chat, where real cost is known only after the stream completes."""
        self._extra_cost[path] = self._extra_cost.get(path, 0.0) + float(usd)

    def record(self, **kw):
        self._records.append({
            "request_id": kw.get("request_id"),
            "path": kw.get("path"),
            "session_id": kw.get("session_id"),
            "latency_ms": float(kw.get("latency_ms", 0.0)),
            "status": kw.get("status"),
            "est_cost_usd": float(kw.get("est_cost_usd", 0.0)),
        })

    def summary(self) -> dict:
        recs = self._records
        n = len(recs)
        if n == 0:
            extra = round(sum(self._extra_cost.values()), 6)
            return {"count": 0, "avg_latency_ms": 0.0, "p95_latency_ms": 0.0,
                    "by_path": {p: {"count": 0, "total_est_cost_usd": round(c, 6)}
                                for p, c in self._extra_cost.items()},
                    "total_est_cost_usd": extra}
        lats = sorted(r["latency_ms"] for r in recs)
        by_path = {}
        for r in recs:
            p = by_path.setdefault(r["path"], {"count": 0, "avg_latency_ms": 0.0,
                                               "total_est_cost_usd": 0.0, "_sum": 0.0})
            p["count"] += 1
            p["_sum"] += r["latency_ms"]
            p["total_est_cost_usd"] += r["est_cost_usd"]
        for path, extra in self._extra_cost.items():  # fold in token-based costs
            p = by_path.setdefault(path, {"count": 0, "avg_latency_ms": 0.0, "total_est_cost_usd": 0.0})
            p["total_est_cost_usd"] += extra
        for p in by_path.values():
            if "_sum" in p:
                p["avg_latency_ms"] = round(p.pop("_sum") / p["count"], 2)
            p["total_est_cost_usd"] = round(p["total_est_cost_usd"], 6)
        return {
            "count": n,
            "avg_latency_ms": round(sum(lats) / n, 2),
            "p95_latency_ms": round(_percentile(lats, 0.95), 2),
            "by_path": by_path,
            "total_est_cost_usd": round(sum(r["est_cost_usd"] for r in recs)
                                        + sum(self._extra_cost.values()), 6),
        }


if __name__ == "__main__":
    # Self-check: summary math (averages, p95, per-path, cost).
    m = Metrics()
    lats = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    for i, lat in enumerate(lats):
        m.record(request_id=str(i), path="/chat" if i % 2 else "/graph",
                 session_id="s", latency_ms=lat,
                 est_cost_usd=estimate_cost(1000, 500))
    s = m.summary()
    assert s["count"] == 10
    assert s["avg_latency_ms"] == 55.0, s
    assert s["p95_latency_ms"] == 100, s   # nearest-rank of 10 -> index 9
    assert set(s["by_path"]) == {"/chat", "/graph"}
    assert s["by_path"]["/chat"]["count"] == 5

    # cost: 1000 in @ $5/1M + 500 out @ $25/1M = 0.005 + 0.0125 = 0.0175 each
    assert abs(estimate_cost(1000, 500) - 0.0175) < 1e-9
    assert abs(s["total_est_cost_usd"] - 0.0175 * 10) < 1e-6, s

    # model selection changes the rate; cache reads bill ~10% of input
    assert abs(estimate_cost(1000, 500, model="claude-haiku-4-5") - 0.0035) < 1e-9  # 0.001 + 0.0025
    assert abs(estimate_cost(0, 0, cache_read_tokens=1_000_000) - 0.5) < 1e-9        # 5.0 * 0.1

    # add_cost: token-based cost folds into the path + grand total (Phase 4 /metrics)
    m.add_cost("/chat", estimate_cost(2000, 800))  # 0.01 + 0.02 = 0.03
    s2 = m.summary()
    assert abs(s2["by_path"]["/chat"]["total_est_cost_usd"] - (0.0175 * 5 + 0.03)) < 1e-6, s2
    assert abs(s2["total_est_cost_usd"] - (0.0175 * 10 + 0.03)) < 1e-6, s2
    # add_cost works even with zero requests (empty-summary path)
    m2 = Metrics(); m2.add_cost("/chat", 0.03)
    assert abs(m2.summary()["total_est_cost_usd"] - 0.03) < 1e-9

    # empty summary is safe
    assert Metrics().summary()["count"] == 0
    log_line(event="self_check", **s)
    print("obs.py self-check ok")

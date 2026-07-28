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
_FALLBACK_RATE = "claude-opus-4-8"   # 모르는 모델에 임시로 쓰는 단가

# 단가를 모르는 모델은 여기에 모인다. 그냥 fallback으로 계산해버리면 /metrics의
# 금액이 "맞는 숫자"처럼 보이는데 실제로는 다른 모델의 단가다 — 비용 판단이
# 조용히 틀린다. 그래서 계산은 하되(0으로 두면 그것도 거짓말이다) 어느 모델이
# 추정치인지 /metrics가 같이 말한다.
UNPRICED_MODELS: set[str] = set()


def estimate_cost(input_tokens: int, output_tokens: int, model: str = "claude-opus-4-8",
                  cache_read_tokens: int = 0, cache_write_tokens: int = 0,
                  cache_ttl: str = "1h") -> float:
    """USD for one call. The four token buckets bill at different rates:

    input       1x    (uncached prompt)
    output      out
    cache read  0.1x  input
    cache write 2x    input for a 1h breakpoint, 1.25x for the 5m default

    cache_write was missing until 2026-07-28, so /metrics reported the *cheapest*
    part of an expensive first call and zero for its most expensive part.
    papers.document_blocks sets ttl="1h", hence the 2x default here.
    """
    r = _RATES.get(model)
    if r is None:
        if model not in UNPRICED_MODELS:
            UNPRICED_MODELS.add(model)
            log_line(event="rate_unknown", model=model, using=_FALLBACK_RATE,
                     note="obs._RATES에 이 모델의 단가가 없어 다른 모델 단가로 추정한다")
        r = _RATES[_FALLBACK_RATE]
    write_mult = 2.0 if cache_ttl == "1h" else 1.25
    return ((input_tokens / 1_000_000) * r["in"]
            + (output_tokens / 1_000_000) * r["out"]
            + (cache_read_tokens / 1_000_000) * r["in"] * 0.1
            + (cache_write_tokens / 1_000_000) * r["in"] * write_mult)


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
                    "total_est_cost_usd": extra,
                    "unpriced_models": sorted(UNPRICED_MODELS)}
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
            # 비어 있지 않으면 위 금액은 다른 모델 단가로 추정한 값이다
            "unpriced_models": sorted(UNPRICED_MODELS),
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

    # 단가를 모르는 모델: 계산은 하되 어느 것이 추정인지 /metrics가 말해야 한다.
    # 예전엔 조용히 opus-4-8 단가를 써서, 다른 모델을 쓰는 동안에도 금액이
    # "맞는 숫자"처럼 보였다.
    UNPRICED_MODELS.clear()
    guessed = estimate_cost(1_000_000, 0, model="claude-made-up-9")
    assert guessed == _RATES[_FALLBACK_RATE]["in"], guessed      # fallback 단가로 계산
    assert "claude-made-up-9" in UNPRICED_MODELS
    assert Metrics().summary()["unpriced_models"] == ["claude-made-up-9"]
    estimate_cost(1, 0, model="claude-made-up-9")                # 두 번째는 로그 안 남긴다
    assert sorted(UNPRICED_MODELS) == ["claude-made-up-9"]
    UNPRICED_MODELS.clear()
    assert abs(estimate_cost(0, 0, cache_read_tokens=1_000_000) - 0.5) < 1e-9        # 5.0 * 0.1

    # cache WRITE is the expensive one and used to be missing entirely (billed as $0).
    # 1h breakpoint = 2x input; the 5m default = 1.25x.
    assert abs(estimate_cost(0, 0, cache_write_tokens=1_000_000) - 10.0) < 1e-9       # 5.0 * 2
    assert abs(estimate_cost(0, 0, cache_write_tokens=1_000_000, cache_ttl="5m") - 6.25) < 1e-9
    # a cold first question (write) must cost far more than a warm one (read)
    cold = estimate_cost(0, 800, cache_write_tokens=300_000)
    warm = estimate_cost(0, 800, cache_read_tokens=300_000)
    assert cold > warm * 15, (cold, warm)

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

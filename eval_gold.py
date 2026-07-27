"""Reference-grounded GOLD-SET eval with a CI pass/fail gate.

Unlike eval.py (reference-free report, always exits 0), this ships labeled
expectations per question and EXITS 1 if thresholds aren't met — so CI can gate.

Run:
    MOCK_LLM=1 python app.py          # terminal 1 (server)
    MOCK_LLM=1 python eval_gold.py    # terminal 2 (this)

Config via env:
    BASE_URL          default http://127.0.0.1:8000
    MIN_GROUNDED      default 0.8   (fraction of answers with >=1 citation)
    MIN_CONCEPT_COV   default 0.5   (avg |wanted ∩ done.concepts| / |wanted|)
    MIN_KEYWORD_COV   default 0.0   (avg keyword hit-rate; lenient — MOCK answers
                                     are canned and won't hit domain keywords)
    MIN_FAITHFULNESS  default 0.0   (avg llm.judge score /5; 0 => not gated)

Only grounded% + concept-coverage are gated by default, which a healthy MOCK run
satisfies (mock names >=2 graph concepts and emits a citation every turn).
"""
import os
import json
import asyncio
import inspect

import httpx

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")

MIN_GROUNDED = float(os.environ.get("MIN_GROUNDED", "0.8"))
MIN_CONCEPT_COV = float(os.environ.get("MIN_CONCEPT_COV", "0.5"))
MIN_KEYWORD_COV = float(os.environ.get("MIN_KEYWORD_COV", "0.0"))
MIN_FAITHFULNESS = float(os.environ.get("MIN_FAITHFULNESS", "0.0"))
# Refusal rate on the ANSWERABLE gold set — these questions ARE covered by the
# corpus, so a healthy system answers them (low refusal). MOCK never refuses, so
# it scores 0 and passes. Out-of-scope refusal (system SHOULD decline) needs a
# real key — MOCK's canned answer can't refuse — so that check is real-key only.
MAX_REFUSAL = float(os.environ.get("MAX_REFUSAL", "0.34"))

# Phrases that signal the model declined to answer from the papers.
_REFUSAL_MARKERS = ("다루지 않", "다루고 있지 않", "언급되지 않", "논문에 없", "포함되어 있지 않",
                    "찾을 수 없", "제공되지 않", "정보가 없", "cannot answer", "don't cover",
                    "not covered", "no information")


def _is_refusal(text: str) -> bool:
    low = (text or "").lower()
    return any(m.lower() in low for m in _REFUSAL_MARKERS)

# Labeled gold set. Questions in Korean (the product language); must_include_concepts
# are graph.json node ids the mock keys off (see llm.ALIASES), so MOCK lights them up.
GOLD_SET = [
    {
        "question": "세그멘테이션은 전자현미경 이미지 분석에 어떻게 쓰이나요?",
        "must_include_concepts": ["segmentation", "annotation"],
        "must_mention": ["segmentation", "pixel", "deep learning"],
    },
    {
        "question": "메타데이터와 FAIR 원칙이 현미경 데이터에 왜 중요한가요?",
        "must_include_concepts": ["metadata", "fair"],
        "must_mention": ["metadata", "FAIR", "reusable"],
    },
    {
        "question": "어노테이션(라벨링)이 EM 세그멘테이션 학습의 병목인 이유는?",
        "must_include_concepts": ["annotation", "segmentation"],
        "must_mention": ["annotation", "bottleneck", "ground truth"],
    },
    {
        "question": "딥러닝 디노이징은 저선량 전자현미경에 어떻게 도움이 되나요?",
        "must_include_concepts": ["denoising", "imaging-conditions"],
        "must_mention": ["denoising", "low-dose", "noise"],
    },
    {
        "question": "EM 이미지 품질과 라벨에 가장 큰 영향을 주는 이미징 조건은?",
        "must_include_concepts": ["imaging-conditions", "denoising"],
        "must_mention": ["dose", "kV", "detector"],
    },
    {
        "question": "완전한 메타데이터가 재현성에 어떻게 기여하나요?",
        "must_include_concepts": ["metadata", "reproducibility"],
        "must_mention": ["reproducibility", "provenance", "metadata"],
    },
]


def _consume_chat(question):
    """POST /chat, drain the SSE stream, return collected fields. (mirrors eval.py)"""
    out = {"session_id": None, "text": "", "citations": [], "concepts": [],
           "quiz_available": False, "turns": 0, "error": None}
    payload = {"session_id": None, "message": question}
    with httpx.stream("POST", f"{BASE_URL}/chat", json=payload, timeout=120.0) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[len("data:"):].strip())
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "session":
                out["session_id"] = ev.get("session_id")
            elif t == "text":
                out["text"] += ev.get("text", "")
            elif t == "citation":
                out["citations"].append(ev.get("citation", {}))
            elif t == "done":
                out["concepts"] = ev.get("concepts", []) or []
                out["quiz_available"] = ev.get("quiz_available", False)
                out["turns"] = ev.get("turns", 0)
            elif t == "error":
                out["error"] = ev.get("message", "unknown error")
    return out


def _load_judge():
    try:
        from llm import judge as _judge
    except Exception as e:
        print(f"[note] llm.judge unavailable ({e!r}) — skipping model judge.")
        return None
    if inspect.iscoroutinefunction(_judge):
        return lambda q, a, c: asyncio.run(_judge(q, a, c))
    return _judge


def _score(item, res):
    wanted = item["must_include_concepts"]
    got = set(res["concepts"])
    concept_cov = len(set(wanted) & got) / len(wanted) if wanted else 1.0

    low = res["text"].lower()
    kws = item["must_mention"]
    keyword_cov = sum(k.lower() in low for k in kws) / len(kws) if kws else 1.0

    grounded = len(res["citations"]) >= 1
    return {"concept_cov": concept_cov, "keyword_cov": keyword_cov, "grounded": grounded,
            "refused": _is_refusal(res["text"])}


def main():
    judge = _load_judge()
    rows = []
    for item in GOLD_SET:
        try:
            res = _consume_chat(item["question"])
        except Exception as e:
            print(f"[error] /chat failed for {item['question']!r}: {e!r}")
            rows.append({"item": item, "error": str(e)})
            continue
        sc = _score(item, res)
        faith = None
        if judge and res["text"].strip() and not res["error"]:
            try:
                faith = int(judge(item["question"], res["text"], res["citations"]).get("faithfulness"))
            except Exception as e:
                print(f"[note] judge errored: {e!r}")
        rows.append({"item": item, "res": res, "scores": sc, "faith": faith})

    return _report_and_gate(rows)


def _report_and_gate(rows):
    print("\n" + "=" * 78)
    print("GOLD-SET EVAL (CI GATE)")
    print("=" * 78)
    valid = [r for r in rows if r.get("scores")]
    for i, r in enumerate(rows, 1):
        print(f"\n[{i}] {r['item']['question']}")
        if not r.get("scores"):
            print(f"    ERROR: {r.get('error')}")
            continue
        s = r["scores"]
        print(f"    concept_cov={s['concept_cov']:.2f}  keyword_cov={s['keyword_cov']:.2f}"
              f"  grounded={'yes' if s['grounded'] else 'NO'}")
        print(f"    want_concepts={r['item']['must_include_concepts']}  got={r['res']['concepts']}")
        if r["faith"] is not None:
            print(f"    faithfulness={r['faith']}/5")

    n = len(valid) or 1
    avg_concept = sum(r["scores"]["concept_cov"] for r in valid) / n
    avg_keyword = sum(r["scores"]["keyword_cov"] for r in valid) / n
    grounded_frac = sum(r["scores"]["grounded"] for r in valid) / n
    refusal_frac = sum(r["scores"]["refused"] for r in valid) / n
    faiths = [r["faith"] for r in valid if r["faith"] is not None]
    avg_faith = (sum(faiths) / len(faiths)) if faiths else None

    print("\n" + "-" * 78)
    print("AGGREGATE")
    print(f"  answers evaluated : {len(valid)}/{len(rows)}")
    print(f"  avg concept_cov   : {avg_concept:.2f}   (min {MIN_CONCEPT_COV})")
    print(f"  avg keyword_cov   : {avg_keyword:.2f}   (min {MIN_KEYWORD_COV})")
    print(f"  grounded          : {grounded_frac:.2f}   (min {MIN_GROUNDED})")
    print(f"  refusal (answerable): {refusal_frac:.2f}   (max {MAX_REFUSAL})")
    print(f"  avg faithfulness  : {(avg_faith/5 if avg_faith is not None else 0):.2f}"
          f"   (min {MIN_FAITHFULNESS}){'' if avg_faith is not None else '  [judge skipped]'}")

    failures = []
    if len(valid) == 0:
        failures.append("no answers evaluated (server unreachable?)")
    if grounded_frac < MIN_GROUNDED:
        failures.append(f"grounded {grounded_frac:.2f} < {MIN_GROUNDED}")
    if avg_concept < MIN_CONCEPT_COV:
        failures.append(f"concept_cov {avg_concept:.2f} < {MIN_CONCEPT_COV}")
    if refusal_frac > MAX_REFUSAL:
        failures.append(f"refusal {refusal_frac:.2f} > {MAX_REFUSAL} (declining answerable questions)")
    if avg_keyword < MIN_KEYWORD_COV:
        failures.append(f"keyword_cov {avg_keyword:.2f} < {MIN_KEYWORD_COV}")
    if MIN_FAITHFULNESS > 0 and (avg_faith is None or avg_faith / 5 < MIN_FAITHFULNESS):
        failures.append(f"faithfulness {(avg_faith/5 if avg_faith else 0):.2f} < {MIN_FAITHFULNESS}")

    print("-" * 78)
    if failures:
        print("GATE: FAIL")
        for f in failures:
            print(f"  - {f}")
        print("=" * 78)
        return 1
    print("GATE: PASS")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

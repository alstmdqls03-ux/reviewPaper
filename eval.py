"""Grounding + accuracy eval harness ("judge agent") for the review-paper chatbot.

Reference-free: we don't ship gold answers. We score whether each answer is
*grounded* (cited from the corpus), whether citations carry supporting text,
whether concepts link back to graph nodes, and — optionally — a model judge
rates faithfulness 1-5.

Run:
    MOCK_LLM=1 python app.py        # terminal 1 (server)
    MOCK_LLM=1 python eval.py       # terminal 2 (this)

Config via env: BASE_URL (default http://127.0.0.1:8000).
Always exits 0 — this is a report, not a CI gate.

Interface assumptions (reconcile with the real modules):
  - llm.py MAY expose judge(question, answer, citations) -> {"faithfulness":int,"rationale":str}
    (sync or async). If absent/broken, the model judge is skipped gracefully.
"""
import os
import json
import asyncio

import httpx

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")

# Reference-free grounding questions across the 5-PDF corpus.
EVAL_SET = [
    "How is semantic segmentation used to analyze electron microscopy images?",
    "Why do FAIR principles and rich metadata matter for microscopy datasets?",
    "What makes manual annotation a bottleneck for training EM segmentation models?",
    "How does deep-learning denoising help with low-dose electron microscopy?",
    "Which imaging conditions most affect the quality of an EM image and its labels?",
]


def _consume_chat(question):
    """POST /chat, drain the SSE stream, return collected fields."""
    out = {
        "session_id": None,
        "text": "",
        "citations": [],
        "concepts": [],
        "quiz_available": False,
        "turns": 0,
        "error": None,
    }
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
    """Return a callable judge(q, a, cites)->dict or None if unavailable."""
    try:
        from llm import judge as _judge  # noqa
    except Exception as e:  # ImportError or missing attr
        print(f"[note] llm.judge unavailable ({e!r}) — skipping model judge.")
        return None

    if asyncio.iscoroutinefunction(_judge):
        def wrapper(q, a, c):
            return asyncio.run(_judge(q, a, c))
        return wrapper
    return _judge


def _score(res):
    """Rule-based grounding scores for one answer."""
    cites = res["citations"]
    grounded = len(cites) >= 1
    cited_text_support = all(c.get("cited_text", "").strip() for c in cites) if cites else False
    concept_linked = len(res["concepts"]) >= 1
    empty = not res["text"].strip()
    return {
        "grounded": grounded,
        "cited_text_support": cited_text_support,
        "concept_linked": concept_linked,
        "empty_or_refusal": empty or res["error"] is not None,
    }


def main():
    judge = _load_judge()
    rows = []
    for q in EVAL_SET:
        try:
            res = _consume_chat(q)
        except Exception as e:
            print(f"[error] /chat failed for {q!r}: {e!r}")
            rows.append({"q": q, "error": str(e), "scores": None, "faith": None})
            continue

        scores = _score(res)
        faith, rationale = None, ""
        if judge and not scores["empty_or_refusal"]:
            try:
                verdict = judge(q, res["text"], res["citations"])
                faith = int(verdict.get("faithfulness"))
                rationale = verdict.get("rationale", "")
            except Exception as e:
                print(f"[note] judge errored for {q!r}: {e!r}")
        rows.append({"q": q, "res": res, "scores": scores, "faith": faith, "rationale": rationale})

    _report(rows, judged=judge is not None)
    return 0


def _report(rows, judged):
    print("\n" + "=" * 78)
    print("GROUNDING / ACCURACY EVAL REPORT")
    print("=" * 78)
    valid = [r for r in rows if r.get("scores")]
    for i, r in enumerate(rows, 1):
        print(f"\n[{i}] {r['q']}")
        if not r.get("scores"):
            print(f"    ERROR: {r.get('error')}")
            continue
        s = r["scores"]
        chk = lambda b: "yes" if b else "NO "
        print(f"    grounded={chk(s['grounded'])}  cited_text={chk(s['cited_text_support'])}"
              f"  concept_linked={chk(s['concept_linked'])}  empty={chk(s['empty_or_refusal'])}")
        print(f"    citations={len(r['res']['citations'])}  concepts={r['res']['concepts']}")
        if r["faith"] is not None:
            print(f"    faithfulness={r['faith']}/5 — {r['rationale']}")

    print("\n" + "-" * 78)
    n = len(valid) or 1
    grounded_pct = 100 * sum(r["scores"]["grounded"] for r in valid) / n
    concept_pct = 100 * sum(r["scores"]["concept_linked"] for r in valid) / n
    empty_pct = 100 * sum(r["scores"]["empty_or_refusal"] for r in valid) / n
    faiths = [r["faith"] for r in valid if r["faith"] is not None]
    avg_faith = sum(faiths) / len(faiths) if faiths else None
    print("AGGREGATE")
    print(f"  answers evaluated : {len(valid)}/{len(rows)}")
    print(f"  grounded          : {grounded_pct:.0f}%")
    print(f"  concept-linked    : {concept_pct:.0f}%")
    print(f"  empty/refusal     : {empty_pct:.0f}%")
    print(f"  avg faithfulness  : {avg_faith:.1f}/5" if avg_faith is not None
          else f"  avg faithfulness  : n/a ({'no scores' if judged else 'judge skipped'})")
    print("=" * 78)


if __name__ == "__main__":
    raise SystemExit(main())

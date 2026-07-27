"""LLM layer: streaming chat (native citations), quiz generation, and a faithfulness judge.

Runs in MOCK mode when MOCK_LLM=1 or no ANTHROPIC_API_KEY is set — canned but
structurally-real responses so the whole app (sessions, streaming, quiz, load test,
eval) can be exercised end-to-end without a key. Real mode uses claude-opus-4-8.
"""
import asyncio
import json
import os
from pathlib import Path

import papers

MOCK = os.getenv("MOCK_LLM") == "1" or not os.getenv("ANTHROPIC_API_KEY")

QUIZ_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["questions"],
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["question", "options", "answer_index", "explanation", "concept_id", "source"],
                "properties": {
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}, "minItems": 4, "maxItems": 4},
                    "answer_index": {"type": "integer"},
                    "explanation": {"type": "string"},
                    "concept_id": {"type": "string"},
                    "source": {"type": "string"},
                },
            },
        }
    },
}

JUDGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["faithfulness", "rationale"],
    "properties": {
        "faithfulness": {"type": "integer"},
        "rationale": {"type": "string"},
    },
}


# ---- Real-mode lazy singletons (never created in MOCK mode) --------------------
_async_client = None
_sync_client = None


def _aclient():
    global _async_client
    if _async_client is None:
        import anthropic
        _async_client = anthropic.AsyncAnthropic()
    return _async_client


def _sclient():
    global _sync_client
    if _sync_client is None:
        import anthropic
        _sync_client = anthropic.Anthropic()
    return _sync_client


# ---- Streaming chat -----------------------------------------------------------
async def stream_chat(messages: list, system: str):
    """Async generator yielding ("text", str) and ("citation", dict) tuples."""
    if MOCK:
        async for ev in _mock_stream(messages):
            yield ev
        return
    c = _aclient()
    async with c.beta.messages.stream(
        model=papers.MODEL, max_tokens=2048, betas=[papers.FILES_BETA],
        system=system, messages=messages,
    ) as stream:
        async for event in stream:
            if event.type != "content_block_delta":
                continue
            d = event.delta
            if d.type == "text_delta":
                yield ("text", d.text)
            elif d.type == "citations_delta":
                ci = d.citation
                yield ("citation", {
                    "title": getattr(ci, "document_title", None),
                    "start_page": getattr(ci, "start_page_number", None),
                    "end_page": getattr(ci, "end_page_number", None),
                    "cited_text": getattr(ci, "cited_text", None),
                })
        # After the stream, surface token usage so the caller can price the turn.
        # MOCK never reaches here, so cost stays $0 offline (honest — mock is free).
        u = (await stream.get_final_message()).usage
        yield ("usage", {
            "input_tokens": getattr(u, "input_tokens", 0) or 0,
            "output_tokens": getattr(u, "output_tokens", 0) or 0,
            "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        })


_GRAPH_NODES = None


def _nodes():
    global _GRAPH_NODES
    if _GRAPH_NODES is None:
        _GRAPH_NODES = json.loads(Path("graph.json").read_text())["nodes"]
    return _GRAPH_NODES


async def _mock_stream(messages: list):
    """Deterministic mock answer that name-drops real graph concepts so the graph
    lights up and the quiz gate opens — good enough to demo the full flow offline."""
    question = ""
    for m in reversed(messages):
        if m["role"] == "user":
            c = m["content"]
            question = c if isinstance(c, str) else next(
                (b["text"] for b in c if b.get("type") == "text"), "")
            break
    nodes = _nodes()
    by_id = {n["id"]: n for n in nodes}
    # Korean/English keyword aliases so the demo actually responds to the Korean questions.
    ALIASES = {
        "segmentation": ["세그멘테이션", "분할", "segmentation"],
        "deep-learning": ["딥러닝", "deep learning"],
        "metadata": ["메타데이터", "metadata"],
        "fair": ["fair", "재현성", "reproducib"],
        "reproducibility": ["재현성", "reproducib"],
        "annotation": ["어노테이션", "라벨", "주석", "annotation"],
        "imaging-conditions": ["이미징", "조건", "imaging condition"],
        "denoising": ["디노이징", "노이즈", "denois"],
        "atom-detection": ["원자", "결함", "atom", "defect"],
        "connectomics": ["커넥토믹스", "connectom"],
        "metadata-standards": ["표준", "standard"],
    }
    low = question.lower()
    ids = [nid for nid, keys in ALIASES.items() if nid in by_id and any(k in low for k in keys)]
    # pad so we always name >=2 concepts (learning-by-linking vibe); greetings get generic ones
    for d in ("segmentation", "annotation"):
        if len(ids) >= 2:
            break
        if d not in ids:
            ids.append(d)
    picked = [by_id[i] for i in ids]
    labels = [n["label"].split(" /")[0] for n in picked]
    link = f"이는 {', '.join(labels[1:])}와(과) 밀접하게 연결됩니다. " if len(labels) > 1 else ""
    answer = (
        f"[데모 응답] 질문하신 내용은 리뷰 논문들에서 {labels[0]} 관점으로 다뤄집니다. {link}"
        f"실제 답변은 API 키를 넣으면 원문 페이지 인용과 함께 생성됩니다."
    )
    for word in answer.split(" "):
        yield ("text", word + " ")
        await asyncio.sleep(0.01)
    yield ("citation", {
        "title": picked[0]["sources"][0] if picked[0].get("sources") else "Review paper",
        "start_page": 3, "end_page": 3,
        "cited_text": f"...{labels[0]} is a central theme discussed across the corpus...",
    })


# ---- Quiz generation ----------------------------------------------------------
async def make_quiz(doc_blocks: list, concept_infos: list) -> list:
    """Return a list of full question dicts (with answer_index + explanation)."""
    if MOCK:
        return _mock_quiz(concept_infos)
    prompt = (
        "Create a short multiple-choice quiz (one question per concept below, max 5) that tests a "
        "new researcher's understanding, grounded ONLY in the attached papers. For each: 4 options, "
        "exactly one correct (answer_index), a one-sentence explanation, the concept_id it tests, and "
        "the source paper title. Concepts:\n"
        + "\n".join(f"- {c['id']} ({c['label']}): {c.get('summary','')}" for c in concept_infos)
    )
    content = list(doc_blocks) + [{"type": "text", "text": prompt}]
    c = _aclient()
    resp = await c.beta.messages.create(
        model=papers.MODEL, max_tokens=4000, betas=[papers.FILES_BETA],
        output_config={"format": {"type": "json_schema", "schema": QUIZ_SCHEMA}},
        messages=[{"role": "user", "content": content}],
    )
    text = next(b.text for b in resp.content if b.type == "text")
    qs = json.loads(text)["questions"]
    for i, q in enumerate(qs):
        q["id"] = f"q{i+1}"
    return qs


def _mock_quiz(concept_infos: list) -> list:
    qs = []
    pool = concept_infos[:4] or [{"id": "segmentation", "label": "Segmentation",
                                  "summary": "pixel labeling", "sources": ["Review paper"]}]
    for i, c in enumerate(pool):
        label = c["label"]
        src = (c.get("sources") or ["Review paper"])[0]
        qs.append({
            "id": f"q{i+1}",
            "question": f"[데모 문제] '{label}'에 대한 다음 설명 중 리뷰 논문의 내용과 가장 부합하는 것은?",
            "options": [
                f"{label}은(는) 리뷰 논문에서 다루지 않는다",
                f"{label}은(는) {c.get('summary','핵심 개념')[:40]}",
                f"{label}은(는) 전자현미경과 무관하다",
                f"{label}은(는) 오직 광학현미경에만 적용된다",
            ],
            "answer_index": 1,
            "explanation": f"{label}: {c.get('summary','')} (데모 채점 — 실제 근거는 API 키 연결 시 원문에서 생성)",
            "concept_id": c["id"],
            "source": src,
        })
    return qs


# ---- Faithfulness judge (used by eval.py) -------------------------------------
def judge(question: str, answer: str, citations: list) -> dict:
    """Rate how well the answer is supported by its cited text. 1-5 + one-line why."""
    if MOCK:
        grounded = len(citations) > 0 and bool(answer.strip())
        return {"faithfulness": 4 if grounded else 2,
                "rationale": "MOCK: grounded with citations" if grounded else "MOCK: no citations"}
    cited = "\n".join(f"- [{c.get('title')}] {c.get('cited_text','')}" for c in citations) or "(none)"
    prompt = (
        "You are a strict grader. Given a question, an answer, and the passages the answer cited, "
        "rate 1-5 how faithfully every claim in the answer is supported by the cited passages "
        "(5=fully supported, 1=unsupported/hallucinated). One-sentence rationale.\n\n"
        f"Q: {question}\n\nAnswer: {answer}\n\nCited passages:\n{cited}"
    )
    c = _sclient()
    resp = c.messages.create(
        model=papers.MODEL, max_tokens=500,
        output_config={"format": {"type": "json_schema", "schema": JUDGE_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


if __name__ == "__main__":
    async def _demo():
        msgs = [{"role": "user", "content": "세그멘테이션 정확도를 좌우하는 요인은?"}]
        chunks = [ev async for ev in stream_chat(msgs, "sys")]
        text = "".join(t for k, t in chunks if k == "text")
        cites = [c for k, c in chunks if k == "citation"]
        assert text and cites, "mock stream should yield text + a citation"
        quiz = await make_quiz([], _nodes()[:4])
        assert len(quiz) == 4 and all("answer_index" in q for q in quiz)
        v = judge("q", text, cites)
        assert 1 <= v["faithfulness"] <= 5
        print(f"llm.py self-check ok (MOCK={MOCK}): {len(text)} chars, {len(cites)} cite, {len(quiz)} quiz Q")
    asyncio.run(_demo())

"""Review-paper study chatbot — production backend.

Long-context RAG (no vector DB by default): review papers live in Claude's context via
the Files API; answers are grounded with native page-level citations, streamed over SSE.
On top of the chat:
  - sessions (in-memory) give multi-turn context and gate quizzes
  - W2 learning depth : per-device concept mastery, spaced-repetition quizzes, notes/export
  - W3 corpus         : growable corpus + user PDF upload + BM25 hybrid doc selection
  - W4 observability  : structured request logging + /metrics
  - production layer  : accounts, resumable persisted conversations (SQLite), rate limiting,
                        request validation, health/readiness, admin analytics, Docker

Runs fully offline in MOCK mode (MOCK_LLM=1).

    MOCK_LLM=1 python app.py      # offline demo
    python app.py                 # real, needs ANTHROPIC_API_KEY
"""
import json
import math
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (BackgroundTasks, FastAPI, File, Form, Header, HTTPException,
                     Request, UploadFile)
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import corpus
import llm
import mastery
import obs
import papers
from accounts import Accounts
from analytics import Analytics
from config import settings
from history import History
from limits import RateLimiter, check_message, check_upload_size
from session import SessionStore, match_concepts

SYSTEM = (
    "You help a new electron-microscopy researcher study a corpus of review papers. "
    "Answer only from the attached papers. Be concise and concrete. When a concept connects "
    "to related concepts across the papers, say so explicitly so the reader can learn by "
    "following the links — that is the point of this tool.\n"
    "Grounding rule (strict): every factual claim must come from the attached documents and "
    "carry a citation. If the attached papers do not cover the question, reply exactly "
    "'선택한 소스에서 다루지 않는 내용입니다.' and then name what the sources DO cover that is "
    "closest to the question. Never fill a gap from background knowledge, and never cite a "
    "document for something it does not say."
)

store = SessionStore(ttl=settings.SESSION_TTL)
MASTERY = mastery.MasteryStore()
ACCOUNTS = Accounts()
HIST = History()
ANALYTICS = Analytics()
METRICS = obs.Metrics()
RL = RateLimiter(settings.RATE_LIMIT, settings.RATE_WINDOW)
# 그래프는 corpus.graph_data()가 파일 변경을 감지해 다시 읽는다. 모듈 전역으로
# 한 번만 읽으면 업로드가 그래프를 다시 써도 재시작 전까지 옛 노드를 쓴다 (G19).
def NODES():      return corpus.graph_data()["nodes"]
def EDGES():      return corpus.graph_data()["edges"]
def NODE_BY_ID(): return corpus.graph_data()["by_id"]
UPLOADS = Path("uploads")
_uploaded: list = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _uploaded
    if not llm.MOCK:
        _uploaded = _upload_corpus(papers.client())
        print(f"papers ready: {len(_uploaded)} documents")
    else:
        print("MOCK mode: no papers uploaded, canned LLM responses")
    yield


def _upload_corpus(c):
    reg = corpus.corpus_papers()
    orig = papers.PAPERS
    try:
        papers.PAPERS = reg
        return papers.ensure_uploaded(c)
    finally:
        papers.PAPERS = orig


app = FastAPI(lifespan=lifespan)

_NO_LIMIT = ("/healthz", "/readyz", "/metrics")


@app.middleware("http")
async def gate(request: Request, call_next):
    # Rate-limit expensive mutations (POST); reads and health checks pass through.
    if request.method != "GET" and not request.url.path.startswith(_NO_LIMIT):
        key = request.headers.get("X-Device-Id") or (request.client.host if request.client else "anon")
        if not RL.allow(key):
            ra = str(int(math.ceil(RL.retry_after(key))))
            return JSONResponse({"detail": "요청이 너무 잦아요. 잠시 후 다시 시도해 주세요."},
                                status_code=429, headers={"Retry-After": ra})
    rid = uuid.uuid4().hex[:8]
    t0 = time.perf_counter()
    response = await call_next(request)
    dt = (time.perf_counter() - t0) * 1000
    METRICS.record(request_id=rid, path=request.url.path, session_id="",
                   latency_ms=dt, status=response.status_code, est_cost_usd=0.0)
    obs.log_line(event="request", request_id=rid, path=request.url.path,
                 status=response.status_code, latency_ms=round(dt, 1))
    return response


def dev(x_device_id: str | None) -> str:
    return x_device_id or "anon"


def learner(x_device_id: str | None) -> str:
    """Storage key for learning progress = the ACCOUNT (user_id), not the raw device.
    So progress follows the account across devices/browsers once they're linked."""
    return ACCOUNTS.resolve(dev(x_device_id))


def _pick_sources(sources: list[str] | None,
                  user_id: str | None = None) -> tuple[list[dict], list[str]]:
    """(uploaded docs to send, their titles) for a selection of source ids.

    Scoped to what `user_id` may see (shared papers + their own uploads), so a
    selection can never reach into someone else's PDF even if its id is guessed.
    sources=None means "no selection sent" = everything visible. An EMPTY list is a
    real selection of nothing and raises — it used to fall through to `or reg` and
    silently answer from all 10 papers right after the user unchecked them all.
    Titles come from the registry, so they name what we SEND, not what got cited.
    """
    if sources is not None and not sources:
        raise ValueError("소스를 1개 이상 선택해 주세요.")
    # 아직 처리 중인 소스는 근거가 될 수 없다 (Files API에 안 올라갔고 추출도 전이다).
    # 조용히 빼면 "고른 소스가 답변에 없다"가 되므로, 그것만 골랐으면 이유를 말한다.
    reg = [e for e in corpus.visible_corpus(user_id) if e.get("status", "ready") == "ready"]
    want = set(sources) if sources else None
    picked = [e for e in reg if want is None or e["id"] in want]
    if not picked:
        pending = [e for e in corpus.visible_corpus(user_id)
                   if e.get("status") == "processing" and (want is None or e["id"] in want)]
        if pending:
            raise ValueError(f"고른 소스가 아직 처리 중이에요 ({len(pending)}편). "
                             "목록에서 처리가 끝나면 다시 시도해 주세요.")
        raise ValueError("선택한 소스를 찾을 수 없어요. 소스 목록을 새로고침해 주세요.")
    # 고른 소스가 모델 창에 안 들어가면 여기서 막는다. 안 막으면 실키에서 API가
    # 400을 던지고, 사용자는 무엇을 줄여야 하는지 모르는 채로 실패를 본다.
    corpus.ensure_estimates(picked)
    total = sum(e.get("est_tokens") or 0 for e in picked)
    if total > settings.MAX_CONTEXT_TOKENS:
        big = max(picked, key=lambda e: e.get("est_tokens") or 0)
        raise ValueError(
            f"선택한 소스 {len(picked)}편이 너무 커요 (약 {total // 1000:,}k 토큰 / "
            f"한도 {settings.MAX_CONTEXT_TOKENS // 1000:,}k). 몇 편 해제하고 다시 시도해 주세요. "
            f"가장 큰 소스: “{big['title'][:60]}” 약 {(big.get('est_tokens') or 0) // 1000:,}k 토큰")
    paths = {e["path"] for e in picked}
    return [u for u in _uploaded if u.get("path") in paths], [e["title"] for e in picked]


def _doc_blocks_for(query: str, citations: bool, cache_last: bool,
                    sources: list[str] | None = None,
                    user_id: str | None = None) -> tuple[list, list[str]]:
    docs, titles = _pick_sources(sources, user_id)
    if not docs:
        return [], titles
    if sources:
        # Explicit selection wins: send exactly what the user checked, in registry order.
        # ponytail: this also keeps the prompt prefix byte-stable across questions, so the
        # 1h cache actually READS instead of re-writing at 2x. BM25 reordering broke that.
        sel = docs
    else:
        sel = corpus.select_documents(query, docs)
        titles = [d["title"] for d in sel]
    return papers.document_blocks(sel, citations=citations, cache_last=cache_last), titles


class ChatIn(BaseModel):
    session_id: str | None = None
    conversation_id: str | None = None
    message: str
    sources: list[str] | None = None  # selected source ids; None = all


class QuizIn(BaseModel):
    session_id: str
    sources: list[str] | None = None


class GradeIn(BaseModel):
    session_id: str
    quiz_id: str
    answers: list[int]


class NoteIn(BaseModel):
    text: str
    source: str = ""
    concept_id: str = ""
    conversation_id: str | None = None      # 어느 대화에서 저장했는지
    citations: list[dict] | None = None     # 답변에 붙어 있던 각주 (offset·쪽·구절 포함)


class NameIn(BaseModel):
    name: str


class ClaimIn(BaseModel):
    token: str


class MarkIn(BaseModel):
    concept_id: str
    known: bool = True


def _build_messages(sess, doc_blocks: list, new_text: str) -> list:
    turns = sess.messages + [{"role": "user", "content": new_text}]
    msgs = [{"role": t["role"], "content": t["content"]} for t in turns]
    for m in msgs:
        if m["role"] == "user":
            m["content"] = list(doc_blocks) + [{"type": "text", "text": m["content"]}]
            break
    return msgs


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


@app.post("/chat")
async def chat(body: ChatIn, x_device_id: str = Header(None)):
    try:
        message = check_message(body.message, settings.MAX_MESSAGE_CHARS)
    except ValueError as e:
        raise HTTPException(400, str(e))
    sess = store.get_or_create(body.session_id)
    device = dev(x_device_id)
    user_id = ACCOUNTS.resolve(device)
    conv_id = body.conversation_id
    if not conv_id:
        conv_id = HIST.start_conversation(user_id)
    elif not sess.messages:  # resuming a persisted conversation: rehydrate live context
        sess.messages = [{"role": m["role"], "content": m["content"]}
                         for m in HIST.get_messages(conv_id)]
        for m in sess.messages:  # restore turn count + covered concepts so quiz gating survives resume
            if m["role"] == "assistant":
                sess.turns += 1
                sess.covered_concepts |= set(match_concepts(m["content"], NODES()))

    # 이 대화의 소스 묶음을 서버에 붙인다 (= 노트북). 클라이언트가 선택을 보내면 그걸
    # 저장하고, 안 보내면 저장돼 있던 것을 쓴다 — 그래야 다른 기기에서 열어도 같은 소스다.
    sources = body.sources
    if sources is None:
        sources = HIST.get_sources(conv_id)
    else:
        HIST.set_sources(conv_id, sources)

    try:
        doc_blocks, used_titles = _doc_blocks_for(message, citations=True, cache_last=True,
                                                  sources=sources, user_id=user_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # Citations name a document by title; the viewer needs its source id to fetch pages.
    title_to_id = {e["title"]: e["id"] for e in corpus.visible_corpus(user_id)}

    async def gen():
        yield _sse({"type": "session", "session_id": sess.id, "conversation_id": conv_id})
        yield _sse({"type": "sources", "titles": used_titles})
        parts, citations = [], []
        async with sess.lock:
            try:
                messages = _build_messages(sess, doc_blocks, message)
                async for kind, payload in llm.stream_chat(messages, SYSTEM):
                    if kind == "text":
                        parts.append(payload)
                        yield _sse({"type": "text", "text": payload})
                    elif kind == "usage":  # real-mode token cost -> /metrics (MOCK never emits this)
                        METRICS.add_cost("/chat", obs.estimate_cost(
                            payload["input_tokens"], payload["output_tokens"],
                            model=settings.MODEL,
                            cache_read_tokens=payload.get("cache_read_tokens", 0),
                            cache_write_tokens=payload.get("cache_write_tokens", 0)))
                    else:
                        payload["source_id"] = title_to_id.get(payload.get("title") or "")
                        # 각주가 답변의 몇 번째 글자 뒤에 붙었는지. 이게 없어서 대화를
                        # 다시 열면 각주가 전부 답변 끝에 뭉쳐 있었다 (실패경계 §4-3).
                        payload["offset"] = sum(len(p) for p in parts)
                        citations.append(payload)
                        yield _sse({"type": "citation", "citation": payload})
                answer = "".join(parts)
                concepts = match_concepts(answer, NODES())
                sess.add_user(message)
                sess.add_assistant(answer, concepts)
                MASTERY.record_covered(user_id, concepts)
                HIST.append(conv_id, "user", message)
                # citations ride along so replaying this conversation restores its footnotes
                HIST.append(conv_id, "assistant", answer,
                            meta={"citations": citations, "sources": used_titles})
                yield _sse({"type": "done", "concepts": concepts,
                            "quiz_available": sess.quiz_available, "turns": sess.turns})
            except Exception as e:
                yield _sse({"type": "error", "message": f"{type(e).__name__}: {e}"})

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/quiz")
async def quiz(body: QuizIn, x_device_id: str = Header(None)):
    sess = store.get_or_create(body.session_id)
    if not sess.quiz_available:
        raise HTTPException(400, "아직 퀴즈를 낼 만큼 대화하지 않았어요. 조금 더 대화해 주세요.")
    device = learner(x_device_id)
    covered = [cid for cid in sess.covered_concepts if cid in NODE_BY_ID()]
    order = MASTERY.due_concepts(device, covered)  # spaced repetition: weak/due first
    covered.sort(key=lambda c: order.index(c) if c in order else len(order))
    infos = [NODE_BY_ID()[cid] for cid in covered]
    # Same source selection as chat. Was: the ENTIRE corpus — 11 papers blows past the
    # 1M context window on a real key, and rewrote the cache prefix every quiz.
    try:
        quiz_docs, _ = _doc_blocks_for(" ".join(c["label"] for c in infos), citations=False,
                                       cache_last=True, sources=body.sources, user_id=device)
    except ValueError as e:
        raise HTTPException(400, str(e))
    async with sess.lock:
        questions = await llm.make_quiz(quiz_docs, infos)
    quiz_id = sess.add_quiz(questions)
    public = [{"id": q["id"], "question": q["question"], "options": q["options"],
               "concept_id": q.get("concept_id", ""), "source": q.get("source", "")}
              for q in questions]
    return {"quiz_id": quiz_id, "questions": public}


@app.post("/quiz/grade")
async def grade(body: GradeIn, x_device_id: str = Header(None)):
    sess = store.get_or_create(body.session_id)
    try:
        result = sess.grade_quiz(body.quiz_id, body.answers)
    except KeyError:
        raise HTTPException(404, "quiz not found for this session")
    MASTERY.record_quiz(learner(x_device_id),
                        [{"concept_id": r["concept_id"], "correct": r["correct"]}
                         for r in result["results"] if r.get("concept_id")])
    return result


@app.post("/session/reset")
async def session_reset(body: QuizIn):
    """Wipe the live session (chat/concepts/turns/quizzes), keeping the same id."""
    store.get_or_create(body.session_id).reset()
    return {"ok": True, "session_id": body.session_id}


@app.get("/mastery")
async def get_mastery(x_device_id: str = Header(None)):
    device = learner(x_device_id)
    return {"mastery": MASTERY.get_mastery(device),
            "next_up": MASTERY.next_up(device, NODES(), EDGES())}


@app.get("/dashboard")
async def dashboard(x_device_id: str = Header(None)):
    """Phase 1 learning dashboard: the 7 metrics from 제안서 §02, MOCK-friendly."""
    return MASTERY.dashboard(learner(x_device_id), NODES())


@app.post("/mastery/mark")
async def mark_mastery(body: MarkIn, x_device_id: str = Header(None)):
    """User self-attests understanding of a concept -> promote/demote its mastery."""
    return MASTERY.mark_known(learner(x_device_id), body.concept_id, body.known)


@app.post("/notes")
async def add_note(body: NoteIn, x_device_id: str = Header(None)):
    meta = {"citations": body.citations} if body.citations else None
    return {"note_id": MASTERY.add_note(learner(x_device_id), body.text, body.source,
                                        body.concept_id, conv_id=body.conversation_id,
                                        meta=meta)}


@app.get("/notes")
async def list_notes(x_device_id: str = Header(None)):
    return {"notes": MASTERY.list_notes(learner(x_device_id))}


@app.get("/notes/export")
async def export_notes(x_device_id: str = Header(None)):
    md = MASTERY.export_markdown(learner(x_device_id))
    return PlainTextResponse(md, media_type="text/markdown",
                             headers={"Content-Disposition": 'attachment; filename="notes.md"'})


# ---- accounts + conversation history (persisted) ------------------------------
@app.get("/account")
async def account(x_device_id: str = Header(None)):
    uid = ACCOUNTS.resolve(dev(x_device_id))
    return {"account": ACCOUNTS.get(uid), "token": ACCOUNTS.issue_token(uid)}


@app.post("/account/claim")
async def account_claim(body: ClaimIn, x_device_id: str = Header(None)):
    """Log this device into an existing account via its restore token.
    Links the device to that account and folds this device's anonymous progress
    into it, so learning follows the account across devices/browsers (Phase 2)."""
    target = ACCOUNTS.verify_token(body.token.strip())
    if not target or ACCOUNTS.get(target) is None:
        raise HTTPException(400, "복원 코드가 올바르지 않아요.")
    device = dev(x_device_id)
    prev = ACCOUNTS.resolve(device)          # this device's current (anonymous) account
    ACCOUNTS.link_device(target, device)     # repoint device -> claimed account
    MASTERY.merge_learner(prev, target)      # carry anonymous progress in
    return {"ok": True, "account": ACCOUNTS.get(target), "token": ACCOUNTS.issue_token(target)}


@app.post("/account/name")
async def set_name(body: NameIn, x_device_id: str = Header(None)):
    uid = ACCOUNTS.resolve(dev(x_device_id))
    ACCOUNTS.set_name(uid, body.name)
    return {"ok": True, "account": ACCOUNTS.get(uid)}


@app.get("/conversations")
async def conversations(q: str = "", x_device_id: str = Header(None)):
    """대화 목록. q가 있으면 제목 + 메시지 본문까지 뒤져서 거른다.
    검색 결과에는 매치된 문장 조각(snippet)이 붙는다."""
    uid = ACCOUNTS.resolve(dev(x_device_id))
    if q.strip():
        return {"conversations": HIST.search(uid, q), "query": q}
    return {"conversations": HIST.list_conversations(uid)}


@app.get("/conversations/{conv_id}")
async def conversation_messages(conv_id: str):
    # title travels with the messages so a deep link (/#/chat/{id}) can label the header
    # without first loading the whole conversation list. sources travels too, so opening
    # a conversation restores its source checkboxes — on any device, not just the one
    # that made the selection.
    return {"messages": HIST.get_messages(conv_id), "title": HIST.get_title(conv_id),
            "sources": HIST.get_sources(conv_id)}


@app.delete("/conversations/{conv_id}")
async def delete_conversation(conv_id: str):
    HIST.delete(conv_id)
    return {"ok": True}


# ---- corpus: list + upload ----------------------------------------------------
@app.get("/corpus")
async def get_corpus(x_device_id: str = Header(None)):
    """The sources this user may read: shared papers + their own uploads.
    Order = registry order (stable), so the UI list doesn't reshuffle."""
    uid = learner(x_device_id)
    vis = corpus.ensure_estimates(corpus.visible_corpus(uid))
    return {"sources": [{"id": e["id"], "title": e["title"], "owner": e.get("owner"),
                         "mine": e.get("owner") == uid, "added_at": e.get("added_at"),
                         "pages": e.get("pages"), "est_tokens": e.get("est_tokens"),
                         "status": e.get("status", "ready"), "status_msg": e.get("status_msg")}
                        for e in vis],
            "owned": corpus.owned_count(uid), "max_sources": settings.MAX_SOURCES,
            "max_context_tokens": settings.MAX_CONTEXT_TOKENS}


@app.get("/corpus/{sid}/page/{page}")
async def source_page(sid: str, page: int, x_device_id: str = Header(None)):
    """One page of extracted text from a source you can see — backs the 원문 viewer.

    Text comes from the same pypdf extraction the BM25 index uses (text_cache), so it
    costs nothing per read. Layout is lost; this shows what a page SAYS, not how it looks.
    """
    uid = learner(x_device_id)
    entry = next((e for e in corpus.visible_corpus(uid) if e["id"] == sid), None)
    if entry is None:
        # 지운 소스라면 404가 아니라 툼스톤을 돌려준다. 예전엔 내가 지운 내 파일에
        # "볼 권한이 없어요"가 떴다 — 과거 답변의 인용이 권한 문제처럼 보였다.
        gone = corpus.tombstone(sid, uid)
        if gone:
            return {"id": sid, "title": gone["title"], "deleted": True,
                    "deleted_at": gone["deleted_at"], "page": page, "total_pages": 0, "text": ""}
        raise HTTPException(404, "그 소스를 볼 권한이 없어요.")
    try:
        pages = corpus.extract_pages(entry["path"])
    except Exception as e:  # noqa: BLE001 — missing file / unreadable PDF is a 404, not a 500
        raise HTTPException(404, f"원문을 읽지 못했어요: {type(e).__name__}")
    if not 1 <= page <= len(pages):
        raise HTTPException(404, f"{page}쪽은 없어요 (전체 {len(pages)}쪽).")
    return {"id": sid, "title": entry["title"], "page": page,
            "total_pages": len(pages), "text": pages[page - 1][1]}


@app.get("/suggestions")
async def suggestions(sources: str = "", x_device_id: str = Header(None)):
    """Starter questions derived from the SELECTED sources, not a hardcoded list.

    Built from graph.json (concepts already tagged with the paper they came from), so
    this is a pure lookup — no model call, no cost. Concepts the learner hasn't covered
    come first, since those are the ones worth asking about.
    """
    uid = learner(x_device_id)
    ids = [s for s in sources.split(",") if s]
    try:
        _, titles = _pick_sources(ids or None, uid)
    except ValueError:
        return {"suggestions": [], "from_sources": 0}  # 고른 소스가 없으면 추천도 없다
    tset = set(titles)
    # 겹치는 개념이 없으면 빈 목록으로 나간다. 예전엔 `or NODES()`로 전체 그래프를
    # 되돌렸는데, 그러면 방금 체크 해제한 논문의 개념이 추천칩에 뜬다 — 내가 올린
    # PDF 한 편만 고른 순간 화면이 공용 논문 얘기를 시작하는 동작이었다.
    hits = [n for n in NODES() if tset & set(n.get("sources") or [])]
    known = set(MASTERY.get_mastery(uid) or {})
    hits.sort(key=lambda n: n["id"] in known)  # 아직 안 배운 개념 먼저
    out = [f"{n['label'].split(' /')[0]}에 대해 설명해줘" for n in hits[:3]]
    # 두 개념을 잇는 질문 하나 — 이 앱의 목적이 "연결해서 배우기"라서
    ids_in = {n["id"] for n in hits}
    link = next((e for e in EDGES() if e["source"] in ids_in and e["target"] in ids_in
                 and e["source"] != e["target"]), None)
    if link:
        a, b = NODE_BY_ID().get(link["source"]), NODE_BY_ID().get(link["target"])
        if a and b:
            out.append(f"{a['label'].split(' /')[0]}와(과) {b['label'].split(' /')[0]}는 어떻게 연결되나?")
    return {"suggestions": out[:4], "from_sources": len(tset)}


@app.delete("/corpus/{sid}")
async def delete_source(sid: str, x_device_id: str = Header(None)):
    """Delete one of your own uploads. Shared seed papers are not deletable."""
    hit = corpus.remove(sid, learner(x_device_id))
    if hit is None:
        raise HTTPException(404, "내가 올린 소스만 삭제할 수 있어요.")
    return {"ok": True, "id": sid, "title": hit["title"]}


@app.patch("/corpus/{sid}")
async def rename_source(sid: str, body: NameIn, x_device_id: str = Header(None)):
    """Retitle one of your own uploads. The title is what answers cite it by."""
    hit = corpus.rename(sid, body.name, learner(x_device_id))
    if hit is None:
        raise HTTPException(404, "내가 올린 소스만 이름을 바꿀 수 있어요.")
    return {"ok": True, "id": sid, "title": hit["title"]}


@app.post("/upload")
async def upload(background: BackgroundTasks, file: UploadFile = File(...),
                 title: str = Form(...), x_device_id: str = Header(None)):
    contents = await file.read()
    if not contents:
        raise HTTPException(400, "빈 파일이에요. PDF를 다시 선택해 주세요.")
    if not contents.startswith(b"%PDF"):
        # Trust the bytes, not the extension — a renamed .docx would fail later at extraction.
        raise HTTPException(415, "PDF 파일만 추가할 수 있어요.")
    try:
        check_upload_size(len(contents), settings.MAX_UPLOAD_MB)
    except ValueError as e:
        raise HTTPException(413, str(e))
    owner = learner(x_device_id)
    if corpus.owned_count(owner) >= settings.MAX_SOURCES:
        raise HTTPException(409, f"소스는 최대 {settings.MAX_SOURCES}편까지 추가할 수 있어요. "
                                 "먼저 사용하지 않는 소스를 삭제해 주세요.")
    # Per-owner directory: two users uploading "paper.pdf" must not overwrite each other.
    dest_dir = UPLOADS / owner
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / Path(file.filename).name
    dest.write_bytes(contents)
    # Re-uploading the same filename replaces the bytes, so drop the cached page text
    # or BM25/citations would keep serving the old document's pages.
    Path("text_cache", dest.stem + ".json").unlink(missing_ok=True)
    entry = corpus.add_pdf(str(dest), title, owner=owner, status="processing")
    corpus._reset_index()
    # 느린 일(텍스트 추출 · Files API 업로드 · 개념 그래프 재생성)은 응답을 보낸 뒤
    # 돈다. 전에는 요청 안에서 동기로 처리해서, 실키에서 POST /upload가 그래프
    # 재생성이 끝날 때까지 수십 초 응답하지 않았다 (회고 N3).
    background.add_task(_finish_upload, entry["id"], entry["path"])
    return {"corpus_size": len(corpus.visible_corpus(owner)), "title": title,
            "id": entry["id"], "status": entry.get("status", "processing"),
            "owned": corpus.owned_count(owner), "max_sources": settings.MAX_SOURCES}


def _finish_upload(sid: str, path: str) -> None:
    """업로드 후처리. 동기 함수라 Starlette가 스레드풀에서 돌린다 (이벤트 루프를 막지 않음).

    끝나면 status가 ready 또는 error가 되고, 목록을 폴링하던 화면이 그걸 보고 바뀐다.
    """
    global _uploaded
    try:
        pages, est = corpus.estimate_tokens(path)     # pypdf 추출 (여기가 몇 초)
        db = corpus.connect()
        with db:
            db.execute("UPDATE sources SET pages=?, est_tokens=? WHERE id=?", (pages, est, sid))
        if not pages:
            corpus.set_status(sid, "error", "텍스트를 추출하지 못했어요 (이미지만 있는 PDF일 수 있어요)")
            return
        if not llm.MOCK:
            _uploaded = _upload_corpus(papers.client())   # Files API 업로드
            # ponytail: the concept graph is global, so it is rebuilt from the SHARED
            # papers only — one user's upload must not rewrite everyone else's map.
            # corpus.graph_data()가 파일 변경을 감지하므로 재시작 없이 반영된다.
            corpus.regenerate_graph()
        corpus.set_status(sid, "ready")
    except Exception as e:  # noqa: BLE001 — 실패해도 행은 남기고 사유를 화면에 보여준다
        obs.log_line(event="upload_process_failed", source_id=sid, error=f"{type(e).__name__}: {e}")
        corpus.set_status(sid, "error", f"처리 실패: {type(e).__name__}")


# ---- ops: metrics, analytics, health ------------------------------------------
@app.get("/metrics")
def metrics():
    return METRICS.summary()


def _require_admin(x_admin_token: str | None):
    if settings.ADMIN_TOKEN and x_admin_token != settings.ADMIN_TOKEN:
        raise HTTPException(403, "admin token required")


@app.get("/analytics")
async def analytics(x_admin_token: str = Header(None)):
    _require_admin(x_admin_token)
    return ANALYTICS.summary()


@app.get("/analytics/students")
async def analytics_students(x_admin_token: str = Header(None)):
    """Teacher/TA view: per-student progress + class averages (제안서 Phase 3).
    Reuses the Phase-1 dashboard formulas per learner; the whole cohort is one class."""
    _require_admin(x_admin_token)
    metrics = ("progress_pct", "avg_score", "quiz_accuracy",
               "learning_score", "concepts_learned", "streak_days")
    students = []
    for uid in MASTERY.all_learners():
        d = MASTERY.dashboard(uid, NODES())
        acct = ACCOUNTS.get(uid) or {}
        students.append({"user_id": uid, "name": acct.get("display_name") or "익명",
                         **{k: d[k] for k in metrics}, "quiz_attempts": d["quiz_attempts"]})
    students.sort(key=lambda s: s["learning_score"], reverse=True)
    n = len(students)
    averages = {k: round(sum(s[k] for s in students) / n, 1) for k in metrics} if n else {}
    return {"students": students, "class_average": averages, "student_count": n,
            "total_concepts": len(NODES())}


@app.get("/healthz")
def healthz():
    return {"status": "ok", **settings.summary()}


@app.get("/readyz")
def readyz():
    ready = llm.MOCK or bool(_uploaded)
    return JSONResponse({"ready": ready}, status_code=200 if ready else 503)


@app.get("/graph")
def graph():
    g = corpus.graph_data()
    return {"nodes": g["nodes"], "edges": g["edges"]}


app.mount("/", StaticFiles(directory="static", html=True), name="static")  # must be last


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)

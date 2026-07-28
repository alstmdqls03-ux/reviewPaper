"""Corpus-expansion (W3): a growable paper registry + hybrid BM25 retrieval.

Lifts the hardcoded 5-paper limit. The registry lives in the `sources` table of
app.db and seeds from papers.PAPERS on first use, growing via add_pdf. Uploading
still goes through papers.ensure_uploaded — this module only decides WHAT to
upload/retrieve.

**Registry storage (2026-07-28)**: was `corpus.json`, a whole-file
read-modify-write. Two uploads landing together lost one of them — last write
won, and the losing PDF stayed on disk unregistered. It is now a SQLite table in
the same app.db as conversations, which buys three things the JSON file could
not: atomic add/remove, a real index on `owner`, and a foreign key from
`conversation_sources` so "this conversation's sources" can't point at a paper
that no longer exists. corpus.json is migrated once on first use and then left
alone as a backup — nothing reads it afterwards.

ponytail: BM25 is hand-rolled (no rank-bm25 dep) and pure-Python — it's ~40
lines and unit-testable. Upgrade to embeddings/a vector store only if page-level
lexical recall measurably misses (synonyms, cross-lingual KO↔EN queries).
"""
import hashlib
import json
import math
import re
import sqlite3
import time
from collections import Counter
from pathlib import Path

import papers

_CORPUS = Path("corpus.json")          # 이관 원본 (이관 후에는 아무도 안 읽는다)
_TEXT_CACHE_DIR = Path("text_cache")
_DB_PATH = "app.db"
_db = None

# 이 테이블의 주인은 corpus.py다. history.py도 같은 DDL을 실행하는데(자기 연결로),
# conversation_sources가 sources를 FK로 참조하기 때문이다. CREATE ... IF NOT EXISTS라
# 누가 먼저 돌든 결과가 같고, 그래서 두 모듈의 초기화 순서를 신경 쓰지 않아도 된다.
SOURCES_DDL = """
CREATE TABLE IF NOT EXISTS sources(
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,   -- 같은 경로 두 번 등록 불가를 DB가 보장한다
    title TEXT NOT NULL,
    owner TEXT,                  -- NULL = 공용 시드, 그 외 = 올린 사람의 user_id
    sha TEXT,                    -- 내용 해시: 이름만 바꾼 같은 PDF를 잡는다
    added_at REAL,
    pages INTEGER,               -- 쪽 수 (추출 시점 기준)
    est_tokens INTEGER,          -- 이 논문을 프롬프트에 넣을 때의 대략 토큰 수
    -- 'ready' | 'processing' | 'error'. processing인 소스는 답변 근거로 안 쓴다
    -- (아직 Files API에 안 올라갔거나 텍스트 추출 전이다)
    status TEXT NOT NULL DEFAULT 'ready',
    status_msg TEXT,
    -- 삭제는 행을 지우지 않고 여기에 시각을 찍는다(툼스톤). 파일은 진짜로 지운다.
    -- 행을 지우면 그 논문을 인용한 과거 답변이 "권한이 없어요"가 되어버린다 —
    -- 내가 지운 내 파일에 권한 문구가 뜨는 동작이었다.
    deleted_at REAL
);
CREATE INDEX IF NOT EXISTS idx_sources_owner ON sources(owner);
CREATE INDEX IF NOT EXISTS idx_sources_sha ON sources(sha);
"""

# BM25 knobs — the textbook defaults; fine for page-length docs.
BM25_K1 = 1.5
BM25_B = 0.75
MAX_DOCS = 5  # long-context ceiling: how many papers we hand the model per query


# ---------------------------------------------------------------- registry ---
def source_id(path: str) -> str:
    """Stable short id for a registry entry — what the UI selects/deselects by."""
    return hashlib.sha1(path.encode("utf-8")).hexdigest()[:12]


def connect(db_path: str | None = None):
    """The shared connection to app.db, creating the `sources` table on first use.

    ponytail: same single-connection pattern as history.py/accounts.py — one
    event-loop thread. Ceiling and upgrade path are documented there.
    """
    global _db, _DB_PATH
    if db_path is not None and db_path != _DB_PATH:
        _DB_PATH, _db = db_path, None          # tests point us at a temp file
    if _db is not None:
        return _db
    db = sqlite3.connect(_DB_PATH, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")     # conversation_sources의 FK가 실제로 강제되게
    db.executescript(SOURCES_DDL)
    # pages/est_tokens는 sources가 나온 뒤에 붙었다. 기존 DB는 제자리 ALTER로.
    cols = {r["name"] for r in db.execute("PRAGMA table_info(sources)")}
    for col, typ in (("pages", "INTEGER"), ("est_tokens", "INTEGER"),
                     ("status", "TEXT NOT NULL DEFAULT 'ready'"), ("status_msg", "TEXT"),
                     ("deleted_at", "REAL")):
        if col not in cols:
            db.execute(f"ALTER TABLE sources ADD COLUMN {col} {typ}")
    db.commit()
    _db = db
    _migrate_from_json()
    _backfill_pretty_titles()
    return _db


def _row(r) -> dict:
    """DB 행 -> 기존 JSON 레지스트리와 같은 모양의 dict (호출부가 안 바뀌게)."""
    d = {"id": r["id"], "path": r["path"], "title": r["title"],
         "owner": r["owner"], "sha": r["sha"], "added_at": r["added_at"]}
    keys = r.keys()
    d["pages"] = r["pages"] if "pages" in keys else None
    d["est_tokens"] = r["est_tokens"] if "est_tokens" in keys else None
    d["status"] = (r["status"] if "status" in keys else None) or "ready"
    d["status_msg"] = r["status_msg"] if "status_msg" in keys else None
    d["deleted_at"] = r["deleted_at"] if "deleted_at" in keys else None
    return d


# 문서 블록 1개가 프롬프트에서 차지하는 대략 토큰 수.
# 본문은 영문 논문 기준 ~4자/토큰, 거기에 PDF 쪽마다 레이아웃·이미지 오버헤드가 붙는다.
# 정확한 수가 목적이 아니라 "한도를 넘겼나"를 미리 알기 위한 것이라 보수적으로 잡는다
# (실제보다 크게 잡아야 API가 400을 던지기 전에 우리가 먼저 막는다).
CHARS_PER_TOKEN = 4
PAGE_OVERHEAD_TOKENS = 250


def estimate_tokens(path: str) -> tuple[int, int]:
    """(쪽 수, 추정 토큰). 추출 실패하면 (0, 0) — 막지 않고 통과시킨다."""
    try:
        pages = extract_pages(path)
    except Exception:  # noqa: BLE001 — 추정 실패가 업로드를 막으면 안 된다
        return (0, 0)
    chars = sum(len(t) for _, t in pages)
    return (len(pages), chars // CHARS_PER_TOKEN + len(pages) * PAGE_OVERHEAD_TOKENS)


def ensure_estimates(entries: list[dict]) -> list[dict]:
    """est_tokens가 비어 있는 엔트리만 계산해 저장한다 (엔트리당 딱 한 번).

    ponytail: 등록 시점에 채우고 여기서는 백필만 한다. 매 요청마다 text_cache를
    다시 읽으면 10편에 파일 I/O 10번이라, 한도 검사 한 줄이 채팅보다 비싸진다.
    """
    todo = [e for e in entries if not e.get("est_tokens")]
    if not todo:
        return entries
    db = connect()
    with db:
        for e in todo:
            pages, est = estimate_tokens(e["path"])
            e["pages"], e["est_tokens"] = pages, est
            db.execute("UPDATE sources SET pages=?, est_tokens=? WHERE id=?",
                       (pages, est, e["id"]))
    return entries


def _migrate_from_json() -> int:
    """corpus.json -> sources 테이블, 딱 한 번. 비어 있을 때만 돈다.

    순서를 그대로 옮기는 게 중요하다: rowid 순서가 곧 레지스트리 순서이고,
    그 순서가 프롬프트 문서 블록 순서 = 1h 캐시 프리픽스다 (app._doc_blocks_for).
    순서가 흔들리면 캐시가 매 질문 새로 쓰이고 비용이 5배가 된다.
    """
    import migrations
    # 대상이 비었는지로 판정하면, 사용자가 소스를 전부 지운 다음 기동에 corpus.json이
    # 통째로 되살아난다. "이관을 했다"와 "데이터가 있다"는 별개의 사실이다.
    already = _db.execute("SELECT 1 FROM sources LIMIT 1").fetchone() is not None
    if not migrations.claim(_db, "corpus_json_to_sources", already_done=already):
        return 0
    if _CORPUS.exists():
        try:
            reg = json.loads(_CORPUS.read_text())
        except ValueError:
            reg = []
    else:
        reg = [{"path": p, "title": t} for p, t in papers.PAPERS]
    n = 0
    for e in reg:                                    # JSON 순서 그대로 insert
        _db.execute(
            """INSERT OR IGNORE INTO sources(id,path,title,owner,sha,added_at)
               VALUES(?,?,?,?,?,?)""",
            (e.get("id") or source_id(e["path"]), e["path"], e["title"],
             e.get("owner"), e.get("sha"), e.get("added_at") or time.time()))
        n += 1
    _db.commit()
    return n


def _backfill_pretty_titles() -> int:
    """이미 들어와 있는 파일명 제목을 한 번만 다듬는다.

    새로 등록되는 건 add_pdf가 처리하지만, 기존 행은 그대로라 목록과 출처 줄이
    계속 다른 모양으로 남는다. 원장에 기록해 딱 한 번만 돈다.
    """
    import migrations
    if not migrations.claim(_db, "pretty_filename_titles"):
        return 0
    n = 0
    for r in _db.execute("SELECT id, title FROM sources").fetchall():
        nice = pretty_title(r["title"])
        if nice != r["title"]:
            _db.execute("UPDATE sources SET title=? WHERE id=?", (nice, r["id"]))
            n += 1
    _db.commit()
    return n


def load_corpus() -> list[dict]:
    """The FULL registry as [{"id","path","title","owner","sha","added_at"}].

    owner is None for the seeded/shared papers everyone sees, or a user_id for a
    PDF someone uploaded. Callers that serve a user want visible_corpus() instead.
    Order is insertion order (rowid) — the same order corpus.json had, and the
    order the prompt's document blocks are built in. Do not change it casually.
    """
    return [_row(r) for r in connect().execute("SELECT * FROM sources WHERE deleted_at IS NULL ORDER BY rowid")]


def visible_corpus(user_id: str | None = None) -> list[dict]:
    """What one user may see: the shared seed papers + their own uploads.
    user_id=None means "no user context" -> shared papers only."""
    return [_row(r) for r in connect().execute(
        "SELECT * FROM sources WHERE deleted_at IS NULL AND (owner IS NULL OR owner = ?) "
        "ORDER BY rowid", (user_id,))]


def content_hash(path: str) -> str:
    """sha1 of the file's bytes, or "" if unreadable."""
    try:
        return hashlib.sha1(Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""


def pretty_title(title: str) -> str:
    """파일명이 그대로 제목이 된 경우만 사람이 읽는 형태로 바꾼다.

    업로드 창의 제목을 비우면 파일명이 제목이 된다. 그러면 목록에는
    `a-review-of-the-grain-bou…`로 잘려 나오고 답변 위 출처 줄에는 119자 전문이
    나와서, 둘이 같은 논문인지 화면만 봐서는 알 수 없었다. 확장자와 하이픈만
    걷어내도 두 곳이 같은 문장으로 시작한다.

    사람이 직접 쓴 제목은 건드리지 않는다 — 공백이 있으면 파일명이 아니다.
    """
    t = (title or "").strip()
    if not t or " " in t:
        return t
    t = re.sub(r"\.(pdf|PDF)$", "", t)
    t = re.sub(r"[-_]+", " ", t).strip()
    t = re.sub(r"\s+", " ", t)
    return t[:1].upper() + t[1:] if t else title


def add_pdf(path: str, title: str, owner: str | None = None,
            status: str = "ready") -> dict:
    """Register an entry and return it (or the existing one). Offline/pure — no upload.

    Dedupes by path AND by file content: the same PDF saved under two names is the
    same paper, and registering it twice doubles its tokens in every request. Only
    compares against entries this owner can see, so users can't probe each other's files.

    경로 중복은 **스키마가 막는다**(`path TEXT UNIQUE`) — 확인 후 삽입 사이의 틈을
    코드로 막으려 하지 않고 `INSERT … ON CONFLICT DO NOTHING`으로 원자적으로 처리한다.
    corpus.json 시절엔 통파일 read-modify-write라, 동시에 두 건이 들어오면 한 건이
    사라지거나(마지막 쓰기 승) 파일 자체가 깨졌다.

    내용(sha) 중복 검사는 조언적이다: 소유자 가시성에 따라 달라지는 조건이라
    UNIQUE로 표현할 수 없다. 극단적 동시성에서 같은 내용이 두 번 들어올 수는 있지만,
    경로 유일성은 어떤 경우에도 깨지지 않는다.
    """
    db = connect()
    title = pretty_title(title)
    sha = content_hash(path)
    hit = db.execute("SELECT * FROM sources WHERE path = ?", (path,)).fetchone()
    if hit and hit["deleted_at"] is None:
        return _row(hit)                   # 이미 등록됨; 업로드는 ensure_uploaded가 지연 처리
    if hit:
        # 툼스톤이 된 경로에 같은 파일을 다시 올렸다. 행을 되살린다 — 새 id를 주면
        # 그 논문을 인용한 과거 답변은 계속 삭제된 id를 가리켜 영영 안 열린다.
        pages, est = (0, 0) if status == "processing" else estimate_tokens(path)
        with db:
            db.execute("""UPDATE sources SET deleted_at=NULL, title=?, owner=?, sha=?,
                                 added_at=?, pages=?, est_tokens=?, status=?, status_msg=NULL
                           WHERE path=?""",
                       (title, owner, sha, time.time(), pages, est, status, path))
        _reset_index()
        return _row(db.execute("SELECT * FROM sources WHERE path = ?", (path,)).fetchone())
    if sha:
        # 내가 볼 수 있는 범위에서만 비교한다 — 해시로 남의 파일 존재를 떠볼 수 없게
        dup = db.execute(
            "SELECT * FROM sources WHERE sha = ? AND deleted_at IS NULL "
            "AND (owner IS NULL OR owner = ?) LIMIT 1",
            (sha, owner)).fetchone()
        if dup:
            return _row(dup)               # 바이트까지 같은 논문이 이미 있다
    # status='processing'이면 추출은 백그라운드가 한다 — 업로드 요청을 붙잡지 않는다
    pages, est = (0, 0) if status == "processing" else estimate_tokens(path)
    entry = {"id": source_id(path), "path": path, "title": title, "owner": owner,
             "added_at": time.time(), "sha": sha, "pages": pages, "est_tokens": est,
             "status": status, "status_msg": None}
    with db:                               # COMMIT / 예외 시 ROLLBACK
        db.execute(
            """INSERT INTO sources(id,path,title,owner,sha,added_at,pages,est_tokens,
                                   status,status_msg)
               VALUES(:id,:path,:title,:owner,:sha,:added_at,:pages,:est_tokens,
                      :status,:status_msg)
               ON CONFLICT(path) DO NOTHING""", entry)
    # 경합에서 졌으면 먼저 들어간 행이 정답이다 (내 dict가 아니라 DB를 믿는다)
    row = db.execute("SELECT * FROM sources WHERE path = ?", (path,)).fetchone()
    return _row(row) if row else entry


def remove(sid: str, owner: str) -> dict | None:
    """Delete an entry the caller OWNS. Returns it, or None if missing/not theirs.

    Shared seed papers (owner=None) are never deletable — one user must not be able
    to empty the corpus for everyone. The PDF on disk goes too; its cached page text
    is dropped so a re-upload of the same name re-extracts rather than serving stale text.

    행은 남기고 `deleted_at`만 찍는다(툼스톤). 목록·근거·검색에서는 즉시 빠지지만,
    그 논문을 인용한 과거 답변의 각주는 제목과 삭제 시각을 계속 보여줄 수 있다.
    행까지 지우면 내가 지운 내 파일에 "볼 권한이 없어요"가 떴다.
    """
    if not owner:
        return None
    db = connect()
    with db:
        hit = db.execute(
            "SELECT * FROM sources WHERE id = ? AND owner = ? AND deleted_at IS NULL",
            (sid, owner)).fetchone()
        if hit is None:
            return None
        db.execute("UPDATE sources SET deleted_at = ? WHERE id = ?", (time.time(), sid))
    hit = _row(hit)
    for p in (Path(hit["path"]), _TEXT_CACHE_DIR / (Path(hit["path"]).stem + ".json")):
        p.unlink(missing_ok=True)
    _reset_index()
    return hit


def rename(sid: str, title: str, owner: str) -> dict | None:
    """Retitle an entry the caller OWNS. The title is what the model cites, so this
    changes how the source is named in future answers (past answers keep the old one)."""
    title = pretty_title(title)
    if not title or not owner:
        return None
    db = connect()
    with db:
        cur = db.execute(
            "UPDATE sources SET title = ? WHERE id = ? AND owner = ? AND deleted_at IS NULL",
            (title[:200], sid, owner))
        if cur.rowcount == 0:
            return None
        hit = db.execute("SELECT * FROM sources WHERE id = ?", (sid,)).fetchone()
    _reset_index()
    return _row(hit)


def set_status(sid: str, status: str, msg: str | None = None) -> None:
    """소스의 처리 상태를 바꾼다. 'ready'가 되기 전에는 답변 근거로 쓰이지 않는다."""
    db = connect()
    with db:
        db.execute("UPDATE sources SET status=?, status_msg=? WHERE id=?", (status, msg, sid))


def claim_orphans(user_id: str) -> int:
    """주인 없는 업로드를 이 사용자에게 귀속시킨다. 돌려주는 건 귀속된 건수.

    v2에서 소유자 격리를 넣기 전에 올라온 PDF는 owner=NULL로 굳었다. 그 값은 "공용
    시드"와 같은 값이라, 그 논문은 **모든 사람** 목록에 '공용' 배지로 뜨고 어느
    계정에서도 이름 변경·삭제 버튼이 안 그려졌다. 올린 사람조차 지울 수 없었다.

    시드와 업로드는 경로로 갈린다 — 시드는 papers/, 업로드는 uploads/. 그래서 플래그
    열을 새로 만들지 않고, 목록을 여는 첫 사용자가 uploads/ 아래의 주인 없는 항목을
    가져간다. 개인 인스턴스에서 "첫 로그인 계정"은 곧 올린 사람이다.

    ponytail: 여러 사람이 쓰는 서버라면 이건 선착순이라 틀릴 수 있다. 그때는 업로드
    시점 로그로 소유자를 복원하고 이 함수를 지워라 — 새로 생기는 데이터는 항상
    owner가 채워지므로, 이 함수의 대상은 시간이 지나도 늘지 않는다.
    """
    if not user_id:
        return 0
    db = connect()
    with db:
        cur = db.execute(
            "UPDATE sources SET owner = ? WHERE owner IS NULL AND path NOT LIKE 'papers/%'",
            (user_id,))
    if cur.rowcount:
        _reset_index()
    return cur.rowcount


def tombstone(sid: str, user_id: str | None = None) -> dict | None:
    """삭제된 소스의 남은 정보 (제목·삭제 시각), 볼 수 있는 사람에게만.

    과거 답변의 인용을 눌렀을 때 "권한이 없어요" 대신 "이 소스는 언제 삭제됐다"를
    보여주기 위한 것. 살아 있는 소스는 여기서 안 나온다 (그건 visible_corpus 몫).
    """
    r = connect().execute(
        """SELECT * FROM sources
            WHERE id = ? AND deleted_at IS NOT NULL AND (owner IS NULL OR owner = ?)""",
        (sid, user_id)).fetchone()
    return _row(r) if r else None


def owned_count(owner: str) -> int:
    return connect().execute(
        "SELECT COUNT(*) FROM sources WHERE owner = ? AND deleted_at IS NULL", (owner,)).fetchone()[0]


def corpus_papers() -> list[tuple]:
    """Registry as papers.PAPERS-shaped tuples, for the shared upload path.
    Every path, all owners — file_ids are per-FILE, so one upload serves everyone
    who can see it; visibility is enforced at selection time, not here."""
    return [(r["path"], r["title"]) for r in connect().execute(
        "SELECT path, title FROM sources WHERE deleted_at IS NULL ORDER BY rowid")]


def shared_papers() -> list[tuple]:
    """Only the shared (owner=None) papers — what the global concept graph is built from,
    so one user's upload can't rewrite everyone else's graph."""
    return [(r["path"], r["title"]) for r in connect().execute(
        "SELECT path, title FROM sources WHERE owner IS NULL AND deleted_at IS NULL "
        "ORDER BY rowid")]


# ------------------------------------------------------------ text extract ---
def _tokenize(text: str) -> list[str]:
    # lowercase, split on non-alphanumeric, drop 1-char noise. Keeps unicode (KO) word chars.
    return [t for t in re.split(r"\W+", text.lower(), flags=re.UNICODE) if len(t) > 1]


def extract_pages(path: str) -> list[tuple]:
    """[(page_no, text)] via pypdf, cached to text_cache/<basename>.json. 1-indexed pages."""
    cache = _TEXT_CACHE_DIR / (Path(path).stem + ".json")
    if cache.exists():
        return [tuple(x) for x in json.loads(cache.read_text())]
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise ImportError("pypdf is required for text extraction: pip install pypdf") from e
    reader = PdfReader(path)
    pages = [(i + 1, (pg.extract_text() or "")) for i, pg in enumerate(reader.pages)]
    _TEXT_CACHE_DIR.mkdir(exist_ok=True)
    cache.write_text(json.dumps(pages))
    return pages


# -------------------------------------------------------------------- BM25 ---
def bm25_rank(query_tokens: list[str], docs_tokens: list[list[str]]) -> list[tuple]:
    """Pure BM25 over pre-tokenized docs. Returns [(idx, score)] sorted desc."""
    n = len(docs_tokens)
    if n == 0:
        return []
    avgdl = sum(len(d) for d in docs_tokens) / n
    df = Counter()
    for d in docs_tokens:
        df.update(set(d))
    scores = []
    for i, d in enumerate(docs_tokens):
        tf = Counter(d)
        dl = len(d)
        s = 0.0
        for q in set(query_tokens):
            if q not in tf:
                continue
            idf = math.log(1 + (n - df[q] + 0.5) / (df[q] + 0.5))
            f = tf[q]
            s += idf * (f * (BM25_K1 + 1)) / (f + BM25_K1 * (1 - BM25_B + BM25_B * dl / avgdl))
        scores.append((i, s))
    scores.sort(key=lambda x: x[1], reverse=True)
    return scores


# Lazy page-level index over the whole corpus: parallel lists so bm25_rank stays generic.
_INDEX = None  # {"meta": [{"path","title","page"}], "tokens": [[tok,...]]}


def _build_index() -> dict:
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    meta, tokens = [], []
    for path, title in corpus_papers():
        for page_no, text in extract_pages(path):
            meta.append({"path": path, "title": title, "page": page_no})
            tokens.append(_tokenize(text))
    _INDEX = {"meta": meta, "tokens": tokens}
    return _INDEX


def _reset_index():
    """Invalidate the cached index (after add_pdf or in tests)."""
    global _INDEX
    _INDEX = None


def select_relevant(query: str, k: int = 6) -> list[dict]:
    """Top-k pages across the corpus: [{"path","title","page","score"}]."""
    idx = _build_index()
    ranked = bm25_rank(_tokenize(query), idx["tokens"])[:k]
    return [{**idx["meta"][i], "score": round(s, 4)} for i, s in ranked if s > 0]


def select_documents(query: str, uploaded: list[dict], max_docs: int = MAX_DOCS) -> list[dict]:
    """Pick which UPLOADED docs ([{file_id,title}]) to feed the model for this query.

    ponytail: when the corpus fits (<= max_docs), hand the model everything — the
    current long-context default, zero retrieval risk. Upgrade path: once the
    corpus outgrows the context window, the BM25 branch below narrows it.
    """
    if len(uploaded) <= max_docs:
        return uploaded
    idx = _build_index()
    agg = Counter()
    for i, s in bm25_rank(_tokenize(query), idx["tokens"]):
        if s > 0:
            agg[idx["meta"][i]["title"]] += s
    top_titles = {t for t, _ in agg.most_common(max_docs)}
    picked = [u for u in uploaded if u["title"] in top_titles]
    # We have room for max_docs — if BM25 hit fewer papers, pad with the rest (in order)
    # so we always fill the budget rather than starve the model of context.
    for u in uploaded:
        if len(picked) >= max_docs:
            break
        if u not in picked:
            picked.append(u)
    return picked[:max_docs]


# ------------------------------------------------------------ graph regen ---
_GRAPH_CACHE = {"stamp": None, "nodes": [], "edges": [], "by_id": {}}


def graph_data() -> dict:
    """graph.json, 파일이 바뀌면 자동으로 다시 읽는다.

    예전엔 app.py가 import 시점에 한 번만 읽어서(`NODES = json.loads(...)`),
    업로드가 regenerate_graph()로 그래프를 다시 써도 프로세스는 재시작 전까지
    옛 노드 목록을 썼다. 화면의 /graph는 디스크를 다시 읽어 새 노드를 보여주는데
    답변의 개념 매칭·"다음에 공부할 개념"·추천 질문은 옛 목록을 쓰는 상태가 됐다
    (docs/benchmark-gap.md G19).

    ponytail: (mtime, size)만 비교한다. stat() 한 번이라 호출당 비용이 무시할 수준이고
    파일이 안 바뀌면 파싱을 건너뛴다. 한계 — 같은 초에 같은 크기로 덮어쓰면 놓친다.
    그런 쓰기는 regenerate_graph()뿐이고 그건 수십 초 걸리는 작업이라 실제로는 안 겹친다.
    """
    p = Path("graph.json")
    try:
        st = p.stat()
        stamp = (st.st_mtime, st.st_size)
    except OSError:
        return _GRAPH_CACHE                      # 파일이 없으면 마지막으로 읽은 것을 유지
    if _GRAPH_CACHE["stamp"] != stamp:
        try:
            g = json.loads(p.read_text())
        except ValueError:                       # 재생성 도중 반쯤 쓰인 파일을 읽었을 수 있다
            return _GRAPH_CACHE
        _GRAPH_CACHE.update(stamp=stamp, nodes=g.get("nodes", []), edges=g.get("edges", []),
                            by_id={n["id"]: n for n in g.get("nodes", [])})
    return _GRAPH_CACHE


def regenerate_graph_needed(prev_titles, cur_titles) -> bool:
    """True if the set of corpus titles changed — graph.json is stale."""
    return set(prev_titles) != set(cur_titles)


def regenerate_graph() -> str:
    """Rebuild graph.json from the SHARED papers via generate_graph.main() (needs API key).

    graph.json is a single global file, so it is built from owner=None entries only —
    otherwise one user's upload would rewrite the concept map everyone else sees.
    """
    try:
        import generate_graph
        orig = papers.PAPERS
        try:
            papers.PAPERS = shared_papers()
            generate_graph.main()
        finally:
            papers.PAPERS = orig
        return "graph regenerated"
    except Exception as e:  # noqa: BLE001 — POC: surface any failure as status, don't crash caller
        return f"graph regen failed: {e}"


# -------------------------------------------------------------- self-check ---
if __name__ == "__main__":
    # (IMPORTANT) 진짜 app.db는 건드리지 않는다 — 임시 DB에서만 돌린다.
    # 예전엔 corpus.json을 백업/복원했지만, 이제 실수로도 앱 레지스트리에 닿지 않는 쪽이 낫다.
    import tempfile as _tf
    _tmp = _tf.mkdtemp()
    _CORPUS = Path(_tmp) / "no-such-corpus.json"   # 이관 원본도 격리 -> papers.PAPERS로 시드된다
    connect(str(Path(_tmp) / "selfcheck.db"))
    try:
        # (a) seeds from papers.PAPERS
        reg = load_corpus()
        n0 = len(papers.PAPERS)
        assert len(reg) == n0, reg              # seeds exactly the registered papers
        assert [e["title"] for e in reg] == [t for _, t in papers.PAPERS]

        # (b) add_pdf dedupes and persists
        e1 = add_pdf("papers/zz-new.pdf", "New Paper")   # nonexistent file -> sha ""
        assert {k: e1[k] for k in ("id", "path", "title", "owner")} == {
            "id": source_id("papers/zz-new.pdf"), "path": "papers/zz-new.pdf",
            "title": "New Paper", "owner": None}
        assert e1["added_at"] > 0 and e1["sha"] == ""
        assert len({e["id"] for e in load_corpus()}) == len(load_corpus()), "ids must be unique"

        assert len(load_corpus()) == n0 + 1
        add_pdf("papers/zz-new.pdf", "New Paper (dupe)")  # same path
        assert len(load_corpus()) == n0 + 1, "dedupe by path failed"

        # (b1) 경로 유일성은 코드가 아니라 스키마가 강제한다 — add_pdf를 우회해도 막힌다.
        # 이게 corpus.json 시절과의 결정적 차이다 (동시 등록에서 한 건이 사라지던 문제).
        import sqlite3 as _sq
        try:
            with _db:
                _db.execute("INSERT INTO sources(id,path,title) VALUES(?,?,?)",
                            ("other-id", "papers/zz-new.pdf", "우회 시도"))
            raise AssertionError("path UNIQUE 제약이 없다 — 같은 논문이 두 번 등록될 수 있다")
        except _sq.IntegrityError:
            pass
        assert len(load_corpus()) == n0 + 1

        # (b2) ownership: seeds are shared, uploads are private to their owner
        add_pdf("uploads/alice.pdf", "Alice's paper", owner="userA")
        add_pdf("uploads/bob.pdf", "Bob's paper", owner="userB")
        a = {e["title"] for e in visible_corpus("userA")}
        b = {e["title"] for e in visible_corpus("userB")}
        assert "Alice's paper" in a and "Bob's paper" not in a, a
        assert "Bob's paper" in b and "Alice's paper" not in b, b
        assert "New Paper" in a and "New Paper" in b, "owner=None stays shared"
        assert {e["title"] for e in visible_corpus(None)} == {e["title"] for e in load_corpus()
                                                             if e.get("owner") is None}
        assert "Alice's paper" not in {t for _, t in shared_papers()}, "graph must skip uploads"
        assert owned_count("userA") == 1 and owned_count("userB") == 1

        # (b3) rename/remove are owner-gated; shared seeds are untouchable
        alice_id = next(e["id"] for e in load_corpus() if e["title"] == "Alice's paper")
        assert rename(alice_id, "Renamed", owner="userB") is None, "userB renamed userA's source"
        assert remove(alice_id, owner="userB") is None, "userB deleted userA's source"
        assert rename(alice_id, "Renamed", owner="userA")["title"] == "Renamed"
        assert "Renamed" in {e["title"] for e in visible_corpus("userA")}
        assert rename(alice_id, "   ", owner="userA") is None, "blank title rejected"
        seed_id = load_corpus()[0]["id"]
        assert remove(seed_id, owner="userA") is None, "shared seed must not be deletable"
        n_before = len(load_corpus())
        assert remove(alice_id, owner="userA")["id"] == alice_id
        assert len(load_corpus()) == n_before - 1
        assert remove(alice_id, owner="userA") is None, "double delete must be a no-op"
        assert owned_count("userA") == 0

        # (b4) 같은 내용의 PDF를 다른 이름으로 올려도 두 번 등록되지 않는다
        real = papers.PAPERS[2][0]
        n_before = len(load_corpus())
        dup = add_pdf(real, "Same bytes, different name", owner="userC")
        assert dup["path"] == real, dup             # 기존 엔트리를 그대로 돌려준다
        assert len(load_corpus()) == n_before, "content dedupe failed"

        # (c) bm25 ranks the obviously-relevant toy doc first
        docs = [
            _tokenize("segmentation of neurons in electron microscopy volumes"),
            _tokenize("metadata provenance and quality control standards"),
            _tokenize("deep learning training loss curves"),
        ]
        ranked = bm25_rank(_tokenize("segmentation neurons"), docs)
        assert ranked[0][0] == 0 and ranked[0][1] > 0, ranked

        # (d) select_documents: ALL when corpus<=max_docs; correct-size subset when >max_docs.
        allp = [{"file_id": f"f{i}", "title": t} for i, (_, t) in enumerate(papers.PAPERS)]
        assert select_documents("segmentation", allp, max_docs=len(allp)) == allp  # fits -> return all

        # Build a synthetic index of 7 papers so >max_docs triggers the BM25 branch.
        titles = [f"Paper {c}" for c in "ABCDEFG"]
        toy_text = {
            "Paper A": "segmentation neurons connectomics em volumes",
            "Paper B": "segmentation deep learning membranes",
            "Paper C": "metadata provenance",
            "Paper D": "materials scanning probe",
            "Paper E": "loss curves training",
            "Paper F": "microscopy imaging modes",
            "Paper G": "unrelated cooking recipe",
        }
        _INDEX = {
            "meta": [{"path": t, "title": t, "page": 1} for t in titles],
            "tokens": [_tokenize(toy_text[t]) for t in titles],
        }
        uploaded7 = [{"file_id": f"f{i}", "title": t} for i, t in enumerate(titles)]
        sub = select_documents("segmentation neurons", uploaded7, max_docs=3)
        assert len(sub) == 3, sub
        assert {u["title"] for u in sub} <= set(titles)
        assert "Paper A" in {u["title"] for u in sub}  # most relevant must survive
        _reset_index()

        # (e) real pypdf extraction on the smallest PDF, else skip.
        try:
            import pypdf  # noqa: F401
            pages = extract_pages("papers/03-microscopy-metadata.pdf")
            assert pages and pages[0][1].strip(), "first page empty"
            print(f"extract_pages ok: {len(pages)} pages")
        except ImportError:
            print("SKIP: pypdf not installed — extract_pages check skipped")

        print("all self-checks passed")
    finally:
        import shutil as _sh
        _sh.rmtree(_tmp, ignore_errors=True)

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
    added_at REAL
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
    db.commit()
    _db = db
    _migrate_from_json()
    return _db


def _row(r) -> dict:
    """DB 행 -> 기존 JSON 레지스트리와 같은 모양의 dict (호출부가 안 바뀌게)."""
    return {"id": r["id"], "path": r["path"], "title": r["title"],
            "owner": r["owner"], "sha": r["sha"], "added_at": r["added_at"]}


def _migrate_from_json() -> int:
    """corpus.json -> sources 테이블, 딱 한 번. 비어 있을 때만 돈다.

    순서를 그대로 옮기는 게 중요하다: rowid 순서가 곧 레지스트리 순서이고,
    그 순서가 프롬프트 문서 블록 순서 = 1h 캐시 프리픽스다 (app._doc_blocks_for).
    순서가 흔들리면 캐시가 매 질문 새로 쓰이고 비용이 5배가 된다.
    """
    if _db.execute("SELECT 1 FROM sources LIMIT 1").fetchone():
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


def load_corpus() -> list[dict]:
    """The FULL registry as [{"id","path","title","owner","sha","added_at"}].

    owner is None for the seeded/shared papers everyone sees, or a user_id for a
    PDF someone uploaded. Callers that serve a user want visible_corpus() instead.
    Order is insertion order (rowid) — the same order corpus.json had, and the
    order the prompt's document blocks are built in. Do not change it casually.
    """
    return [_row(r) for r in connect().execute("SELECT * FROM sources ORDER BY rowid")]


def visible_corpus(user_id: str | None = None) -> list[dict]:
    """What one user may see: the shared seed papers + their own uploads.
    user_id=None means "no user context" -> shared papers only."""
    return [_row(r) for r in connect().execute(
        "SELECT * FROM sources WHERE owner IS NULL OR owner = ? ORDER BY rowid", (user_id,))]


def content_hash(path: str) -> str:
    """sha1 of the file's bytes, or "" if unreadable."""
    try:
        return hashlib.sha1(Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""


def add_pdf(path: str, title: str, owner: str | None = None) -> dict:
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
    sha = content_hash(path)
    hit = db.execute("SELECT * FROM sources WHERE path = ?", (path,)).fetchone()
    if hit:
        return _row(hit)                   # 이미 등록됨; 업로드는 ensure_uploaded가 지연 처리
    if sha:
        # 내가 볼 수 있는 범위에서만 비교한다 — 해시로 남의 파일 존재를 떠볼 수 없게
        dup = db.execute(
            "SELECT * FROM sources WHERE sha = ? AND (owner IS NULL OR owner = ?) LIMIT 1",
            (sha, owner)).fetchone()
        if dup:
            return _row(dup)               # 바이트까지 같은 논문이 이미 있다
    entry = {"id": source_id(path), "path": path, "title": title, "owner": owner,
             "added_at": time.time(), "sha": sha}
    with db:                               # COMMIT / 예외 시 ROLLBACK
        db.execute(
            """INSERT INTO sources(id,path,title,owner,sha,added_at)
               VALUES(:id,:path,:title,:owner,:sha,:added_at)
               ON CONFLICT(path) DO NOTHING""", entry)
    # 경합에서 졌으면 먼저 들어간 행이 정답이다 (내 dict가 아니라 DB를 믿는다)
    row = db.execute("SELECT * FROM sources WHERE path = ?", (path,)).fetchone()
    return _row(row) if row else entry


def remove(sid: str, owner: str) -> dict | None:
    """Delete an entry the caller OWNS. Returns it, or None if missing/not theirs.

    Shared seed papers (owner=None) are never deletable — one user must not be able
    to empty the corpus for everyone. The PDF on disk goes too; its cached page text
    is dropped so a re-upload of the same name re-extracts rather than serving stale text.
    """
    if not owner:
        return None
    db = connect()
    with db:
        hit = db.execute("SELECT * FROM sources WHERE id = ? AND owner = ?",
                         (sid, owner)).fetchone()
        if hit is None:
            return None
        db.execute("DELETE FROM sources WHERE id = ?", (sid,))
    hit = _row(hit)
    for p in (Path(hit["path"]), _TEXT_CACHE_DIR / (Path(hit["path"]).stem + ".json")):
        p.unlink(missing_ok=True)
    _reset_index()
    return hit


def rename(sid: str, title: str, owner: str) -> dict | None:
    """Retitle an entry the caller OWNS. The title is what the model cites, so this
    changes how the source is named in future answers (past answers keep the old one)."""
    title = (title or "").strip()
    if not title or not owner:
        return None
    db = connect()
    with db:
        cur = db.execute("UPDATE sources SET title = ? WHERE id = ? AND owner = ?",
                         (title[:200], sid, owner))
        if cur.rowcount == 0:
            return None
        hit = db.execute("SELECT * FROM sources WHERE id = ?", (sid,)).fetchone()
    _reset_index()
    return _row(hit)


def owned_count(owner: str) -> int:
    return connect().execute(
        "SELECT COUNT(*) FROM sources WHERE owner = ?", (owner,)).fetchone()[0]


def corpus_papers() -> list[tuple]:
    """Registry as papers.PAPERS-shaped tuples, for the shared upload path.
    Every path, all owners — file_ids are per-FILE, so one upload serves everyone
    who can see it; visibility is enforced at selection time, not here."""
    return [(r["path"], r["title"]) for r in connect().execute(
        "SELECT path, title FROM sources ORDER BY rowid")]


def shared_papers() -> list[tuple]:
    """Only the shared (owner=None) papers — what the global concept graph is built from,
    so one user's upload can't rewrite everyone else's graph."""
    return [(r["path"], r["title"]) for r in connect().execute(
        "SELECT path, title FROM sources WHERE owner IS NULL ORDER BY rowid")]


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

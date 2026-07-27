"""Corpus-expansion (W3): a growable paper registry + hybrid BM25 retrieval.

Lifts the hardcoded 5-paper limit. The registry (corpus.json) seeds from
papers.PAPERS on first use and grows via add_pdf. Uploading still goes through
papers.ensure_uploaded — this module only decides WHAT to upload/retrieve.

ponytail: BM25 is hand-rolled (no rank-bm25 dep) and pure-Python — it's ~40
lines and unit-testable. Upgrade to embeddings/a vector store only if page-level
lexical recall measurably misses (synonyms, cross-lingual KO↔EN queries).
"""
import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path

import papers

_CORPUS = Path("corpus.json")
_TEXT_CACHE_DIR = Path("text_cache")

# BM25 knobs — the textbook defaults; fine for page-length docs.
BM25_K1 = 1.5
BM25_B = 0.75
MAX_DOCS = 5  # long-context ceiling: how many papers we hand the model per query


# ---------------------------------------------------------------- registry ---
def source_id(path: str) -> str:
    """Stable short id for a registry entry — what the UI selects/deselects by."""
    return hashlib.sha1(path.encode("utf-8")).hexdigest()[:12]


def load_corpus() -> list[dict]:
    """The FULL registry as [{"id","path","title","owner"}]. Seeds from papers.PAPERS.

    owner is None for the seeded/shared papers everyone sees, or a user_id for a
    PDF someone uploaded. Callers that serve a user want visible_corpus() instead.
    """
    if _CORPUS.exists():
        reg = json.loads(_CORPUS.read_text())
        if any("id" not in e or "owner" not in e for e in reg):  # backfill older registries
            for e in reg:
                e.setdefault("id", source_id(e["path"]))
                e.setdefault("owner", None)  # pre-isolation uploads stay shared
            _CORPUS.write_text(json.dumps(reg, indent=2))
        return reg
    reg = [{"id": source_id(p), "path": p, "title": t, "owner": None} for p, t in papers.PAPERS]
    _CORPUS.write_text(json.dumps(reg, indent=2))
    return reg


def visible_corpus(user_id: str | None = None) -> list[dict]:
    """What one user may see: the shared seed papers + their own uploads.
    user_id=None means "no user context" -> shared papers only."""
    return [e for e in load_corpus() if e.get("owner") in (None, user_id)]


def add_pdf(path: str, title: str, owner: str | None = None) -> dict:
    """Append an entry (dedupe by path), persist, return it. Offline/pure — no upload."""
    reg = load_corpus()
    for e in reg:
        if e["path"] == path:
            return e  # already registered; upload happens lazily via ensure_uploaded
    entry = {"id": source_id(path), "path": path, "title": title, "owner": owner}
    reg.append(entry)
    _CORPUS.write_text(json.dumps(reg, indent=2))
    return entry


def corpus_papers() -> list[tuple]:
    """Registry as papers.PAPERS-shaped tuples, for the shared upload path.
    Every path, all owners — file_ids are per-FILE, so one upload serves everyone
    who can see it; visibility is enforced at selection time, not here."""
    return [(e["path"], e["title"]) for e in load_corpus()]


def shared_papers() -> list[tuple]:
    """Only the shared (owner=None) papers — what the global concept graph is built from,
    so one user's upload can't rewrite everyone else's graph."""
    return [(e["path"], e["title"]) for e in load_corpus() if e.get("owner") is None]


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
    # (IMPORTANT) back up the real corpus.json so asserts can't corrupt the app's registry.
    backup = _CORPUS.read_bytes() if _CORPUS.exists() else None
    try:
        # (a) seeds from papers.PAPERS
        if _CORPUS.exists():
            _CORPUS.unlink()
        reg = load_corpus()
        n0 = len(papers.PAPERS)
        assert len(reg) == n0, reg              # seeds exactly the registered papers
        assert [e["title"] for e in reg] == [t for _, t in papers.PAPERS]

        # (b) add_pdf dedupes and persists
        e1 = add_pdf("papers/zz-new.pdf", "New Paper")
        assert e1 == {"id": source_id("papers/zz-new.pdf"),
                      "path": "papers/zz-new.pdf", "title": "New Paper", "owner": None}
        assert len({e["id"] for e in load_corpus()}) == len(load_corpus()), "ids must be unique"

        assert len(load_corpus()) == n0 + 1
        add_pdf("papers/zz-new.pdf", "New Paper (dupe)")  # same path
        assert len(load_corpus()) == n0 + 1, "dedupe by path failed"

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
        # restore the real registry exactly as we found it
        if backup is None:
            if _CORPUS.exists():
                _CORPUS.unlink()
        else:
            _CORPUS.write_bytes(backup)

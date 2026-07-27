"""Source selection: ids are stable, and a selection narrows what the model reads.

`python test_sources.py` or `pytest -q`. MOCK-safe — no key, no network.
"""
import os

os.environ.setdefault("MOCK_LLM", "1")

import corpus  # noqa: E402


def test_ids_stable_and_unique():
    reg = corpus.load_corpus()
    assert reg, "corpus registry is empty"
    assert all(e.get("id") for e in reg), "every entry needs an id"
    assert len({e["id"] for e in reg}) == len(reg), "source ids must be unique"
    # stable across calls — the UI persists these in localStorage
    assert [e["id"] for e in corpus.load_corpus()] == [e["id"] for e in reg]
    assert corpus.source_id("papers/x.pdf") == corpus.source_id("papers/x.pdf")
    print(f"source ids ok: {len(reg)} entries")


def test_selection_narrows_titles():
    import app

    reg = corpus.load_corpus()
    # no selection -> the whole registry is reported as used
    _, all_titles = app._pick_sources(None)
    assert all_titles == [e["title"] for e in reg], all_titles

    # a two-source selection -> exactly those two titles, in registry order
    want = [reg[0]["id"], reg[2]["id"]]
    _, titles = app._pick_sources(want)
    assert titles == [reg[0]["title"], reg[2]["title"]], titles

    # deselecting a source removes its title
    _, without = app._pick_sources([e["id"] for e in reg if e["id"] != reg[0]["id"]])
    assert reg[0]["title"] not in without
    assert len(without) == len(reg) - 1

    # unknown ids fall back to the whole registry rather than sending nothing
    _, junk = app._pick_sources(["nope"])
    assert junk == all_titles
    print(f"selection ok: {len(all_titles)} -> {len(titles)} titles")


def test_uploads_are_private_to_their_owner():
    """v2 격리: 남이 올린 PDF는 목록에도, 답변 근거에도 들어오지 않는다."""
    import json
    import app

    backup = corpus._CORPUS.read_bytes()
    try:
        corpus.add_pdf("uploads/userA/a.pdf", "A's private paper", owner="userA")
        corpus.add_pdf("uploads/userB/b.pdf", "B's private paper", owner="userB")
        a_titles = [e["title"] for e in corpus.visible_corpus("userA")]
        b_titles = [e["title"] for e in corpus.visible_corpus("userB")]
        assert "A's private paper" in a_titles and "B's private paper" not in a_titles
        assert "B's private paper" in b_titles and "A's private paper" not in b_titles
        # shared seeds stay visible to both
        shared = {t for _, t in corpus.shared_papers()}
        assert shared and shared <= set(a_titles) and shared <= set(b_titles)

        # guessing B's source id from A's session must not surface B's paper
        b_id = next(e["id"] for e in corpus.load_corpus() if e["title"] == "B's private paper")
        _, leaked = app._pick_sources([b_id], user_id="userA")
        assert "B's private paper" not in leaked, leaked
        # ...and an all-invalid selection falls back to A's own visible set, not the world
        assert set(leaked) == set(a_titles), leaked
        print(f"isolation ok: A sees {len(a_titles)}, B sees {len(b_titles)}")
    finally:
        corpus._CORPUS.write_bytes(backup)  # never leave test rows in the real registry
        json.loads(corpus._CORPUS.read_text())


if __name__ == "__main__":
    test_ids_stable_and_unique()
    test_selection_narrows_titles()
    test_uploads_are_private_to_their_owner()
    print("all source self-checks passed")

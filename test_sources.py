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


if __name__ == "__main__":
    test_ids_stable_and_unique()
    test_selection_narrows_titles()
    print("all source self-checks passed")

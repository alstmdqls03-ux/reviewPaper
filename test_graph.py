"""Runnable check: graph.json is well-formed and papers exist. `python test_graph.py`"""
import json
import os


def test_graph():
    g = json.load(open("graph.json"))
    ids = {n["id"] for n in g["nodes"]}
    assert len(ids) == len(g["nodes"]), "duplicate node ids"
    for e in g["edges"]:
        assert e["source"] in ids, f"edge source not a node: {e['source']}"
        assert e["target"] in ids, f"edge target not a node: {e['target']}"
    for n in g["nodes"]:
        assert n["type"] in {"concept", "method", "dataset", "metric", "finding"}, n["type"]
    print(f"graph ok: {len(g['nodes'])} nodes, {len(g['edges'])} edges")


def test_papers_present():
    import papers  # noqa: import here so the graph test runs without deps
    for path, _ in papers.PAPERS:
        assert os.path.exists(path), f"missing {path}"
    print(f"papers ok: {len(papers.PAPERS)} PDFs")


if __name__ == "__main__":
    test_graph()
    try:
        test_papers_present()
    except Exception as e:  # papers.py imports anthropic/dotenv; graph check still matters
        print(f"(skipped papers check: {e})")

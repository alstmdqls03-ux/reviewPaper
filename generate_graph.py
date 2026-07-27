"""One-time: build graph.json — a wiki-style concept graph over the papers.

Uses structured output (a strict JSON schema) so we get valid {nodes, edges} back.
Native Citations can't combine with structured output, so provenance here is
model-filled (paper + section), not verified page citations — that's fine for the
graph; the chat answers carry the verified citations.

Run once after setting ANTHROPIC_API_KEY:  python generate_graph.py
"""
import json

import papers

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["nodes", "edges"],
    "properties": {
        "nodes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "label", "type", "summary", "sources"],
                "properties": {
                    "id": {"type": "string"},
                    "label": {"type": "string"},
                    "type": {"type": "string", "enum": ["concept", "method", "dataset", "metric", "finding"]},
                    "summary": {"type": "string"},
                    "sources": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "edges": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["source", "target", "relation"],
                "properties": {
                    "source": {"type": "string"},
                    "target": {"type": "string"},
                    "relation": {"type": "string",
                                 "enum": ["uses", "improves", "contradicts", "builds_on",
                                          "evaluates_on", "part_of", "related_to"]},
                },
            },
        },
    },
}

PROMPT = (
    "Build a concept graph from the attached electron-microscopy review papers, for a new "
    "researcher learning the field. Extract 15-25 key concepts/methods/datasets as NODES and "
    "the relationships between them as EDGES (edge.source/target reference node.id). "
    "Rules: merge duplicate concepts across papers into ONE node; id = lowercase-hyphenated "
    "slug of the label; one-sentence summaries; for each node list which paper title(s) it came "
    "from in sources[]; only include edges grounded in the text. Return JSON matching the schema."
)


def main():
    c = papers.client()
    uploaded = papers.ensure_uploaded(c)
    content = papers.document_blocks(uploaded, citations=False, cache_last=False)
    content.append({"type": "text", "text": PROMPT})
    resp = c.beta.messages.create(
        model=papers.MODEL,
        max_tokens=8000,
        betas=[papers.FILES_BETA],
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": content}],
    )
    text = next(b.text for b in resp.content if b.type == "text")
    graph = json.loads(text)
    assert graph["nodes"] and graph["edges"], "empty graph"
    ids = {n["id"] for n in graph["nodes"]}
    graph["edges"] = [e for e in graph["edges"] if e["source"] in ids and e["target"] in ids]
    with open("graph.json", "w") as f:
        json.dump(graph, f, indent=2)
    print(f"wrote graph.json: {len(graph['nodes'])} nodes, {len(graph['edges'])} edges")


if __name__ == "__main__":
    main()

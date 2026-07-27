"""Shared corpus config + Files API upload/cache, used by app.py and generate_graph.py."""
import json
import os
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

FILES_BETA = "files-api-2025-04-14"
MODEL = "claude-opus-4-8"  # per claude-api skill default

# The demo corpus: open-access arXiv review papers on ML/metadata in electron microscopy.
# Title is what Claude cites in answers — keep it human-readable.
PAPERS = [
    ("papers/01-deep-learning-in-em.pdf", "Review: Deep Learning in Electron Microscopy (Ede 2021)"),
    ("papers/02-segmentation-survey.pdf", "Segmentation in Large-Scale Cellular EM: A Literature Survey (Aswath 2023)"),
    ("papers/03-microscopy-metadata.pdf", "A Perspective on Microscopy Metadata: Provenance & Quality Control (Huisman 2019)"),
    ("papers/04-connectomics-metadata.pdf", "EM & XRM Connectomics Imaging & Experimental Metadata Standards (Wimbish 2024)"),
    ("papers/05-ml-materials-microscopy.pdf", "ML for Electron & Scanning Probe Microscopy — Mic-Hackathon 2024"),
    ("papers/06-microscopy-image-enhancement-survey.pdf", "Recent Advancements in Microscopy Image Enhancement using Deep Learning: A Survey (Dutta 2025)"),
    ("papers/07-automated-multidim-tem-roadmap.pdf", "A Roadmap for Edge-Computing-Enabled Automated Multidimensional TEM (Mukherjee 2022)"),
    ("papers/08-ai-scientific-inference-nanoparticle-em.pdf", "The Evolution of AI from Image Interpretation toward Scientific Inference in Nanoparticle EM (Toulkeridou 2026)"),
    ("papers/09-superres-microscopy-dl-review.pdf", "Advancing Biological Super-Resolution Microscopy through Deep Learning: A Brief Review (Yang 2021)"),
]

_CACHE = Path("file_cache.json")


def client() -> anthropic.Anthropic:
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY not set. Copy .env.example to .env and add your key.")
    return anthropic.Anthropic()


def ensure_uploaded(c: anthropic.Anthropic) -> list[dict]:
    """Upload each PDF to the Files API once, cache the file_ids on disk.

    Returns a list of {file_id, title, path} in corpus order. ponytail: base64 of all
    5 PDFs is ~47MB, over the 32MB request cap — Files API is the only option.
    """
    cache = json.loads(_CACHE.read_text()) if _CACHE.exists() else {}
    out = []
    for path, title in PAPERS:
        fid = cache.get(path)
        if not fid:
            with open(path, "rb") as f:
                up = c.beta.files.upload(file=(os.path.basename(path), f, "application/pdf"),
                                         betas=[FILES_BETA])
            fid = up.id
            cache[path] = fid
            _CACHE.write_text(json.dumps(cache, indent=2))
            print(f"uploaded {path} -> {fid}")
        out.append({"file_id": fid, "title": title, "path": path})
    return out


def document_blocks(uploaded: list[dict], citations: bool, cache_last: bool) -> list[dict]:
    """Build document content blocks. cache_last puts a 1h cache breakpoint on the
    final doc so the papers (the stable prefix) are cheap to re-read per question."""
    blocks = []
    for i, u in enumerate(uploaded):
        block = {
            "type": "document",
            "source": {"type": "file", "file_id": u["file_id"]},
            "title": u["title"],
        }
        if citations:
            block["citations"] = {"enabled": True}  # all-or-none across docs
        if cache_last and i == len(uploaded) - 1:
            block["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
        blocks.append(block)
    return blocks

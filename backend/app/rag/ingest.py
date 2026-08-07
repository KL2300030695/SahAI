"""
Knowledge-base ingestion: markdown -> chunks -> Chroma + a persisted chunk list.

Chunking is heading-aware. Each chunk keeps the document's front-matter metadata
(`doc_id`, `version`, `effective_from`, `effective_to`), because the
`no_stale_terms` guardrail needs to know a chunk's validity window at retrieval
time -- not at answer time, when it is already too late.

Embeddings run locally through Chroma's bundled ONNX all-MiniLM-L6-v2. That is
deliberate: retrieval is the highest-frequency step in the pipeline, and here it
costs zero tokens and zero dollars.

Run:  python -m app.rag.ingest
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from app.config import BACKEND_DIR, get_settings

KB_DIR = Path(__file__).resolve().parent.parent / "seed" / "kb"
COLLECTION = "sahai_kb"

_FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# Target chunk size in characters. Small enough that a citation shown in the UI
# is readable at a glance; large enough to keep a fee table intact.
MAX_CHARS = 1100
MIN_CHARS = 120


def _store_dir() -> Path:
    s = get_settings()
    p = Path(s.chroma_dir)
    return p if p.is_absolute() else (BACKEND_DIR / p).resolve()


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    m = _FRONT_MATTER.match(text)
    if not m:
        return {}, text
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        meta[k.strip()] = v.strip().strip("'\"")
    return meta, text[m.end() :]


def chunk_markdown(body: str) -> list[str]:
    """Split on '##' headings, then subdivide oversized sections on blank lines."""
    sections: list[str] = []
    current: list[str] = []
    for line in body.splitlines():
        if line.startswith("## ") and current:
            sections.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append("\n".join(current).strip())

    chunks: list[str] = []
    for sec in sections:
        sec = sec.strip()
        if not sec:
            continue
        if len(sec) <= MAX_CHARS:
            chunks.append(sec)
            continue
        # Oversized: split on blank lines, packing paragraphs up to MAX_CHARS.
        heading = sec.splitlines()[0] if sec.startswith("#") else ""
        buf = ""
        for para in re.split(r"\n\s*\n", sec):
            if len(buf) + len(para) + 2 > MAX_CHARS and buf:
                chunks.append(buf.strip())
                buf = f"{heading}\n\n{para}" if heading else para
            else:
                buf = f"{buf}\n\n{para}" if buf else para
        if buf.strip():
            chunks.append(buf.strip())

    return [c for c in chunks if len(c) >= MIN_CHARS]


def build_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(KB_DIR.glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        meta, body = parse_front_matter(raw)
        doc_id = meta.get("doc_id") or path.stem
        for i, chunk in enumerate(chunk_markdown(body)):
            records.append(
                {
                    "chunk_id": f"{doc_id}#{i}",
                    "doc_id": doc_id,
                    "title": meta.get("title", doc_id),
                    "category": meta.get("category", "general"),
                    "version": meta.get("version", "v1"),
                    "effective_from": meta.get("effective_from", ""),
                    # Empty string means "no expiry". Chroma metadata cannot hold None.
                    "effective_to": meta.get("effective_to", ""),
                    "status": meta.get("status", "active"),
                    "source_path": path.name,
                    "text": chunk,
                }
            )
    return records


def ingest(reset: bool = True) -> int:
    import chromadb

    store = _store_dir()
    if reset and store.exists():
        shutil.rmtree(store)
    store.mkdir(parents=True, exist_ok=True)

    records = build_records()
    if not records:
        raise RuntimeError(f"no KB documents found in {KB_DIR}")

    client = chromadb.PersistentClient(path=str(store))
    try:
        client.delete_collection(COLLECTION)
    except Exception:
        pass
    collection = client.create_collection(
        COLLECTION, metadata={"hnsw:space": "cosine"}
    )

    collection.add(
        ids=[r["chunk_id"] for r in records],
        documents=[r["text"] for r in records],
        metadatas=[
            {k: v for k, v in r.items() if k not in ("text", "chunk_id")}
            for r in records
        ],
    )

    # Persist the flat chunk list too: the BM25 half of the hybrid retriever is
    # rebuilt from this at startup, and it makes the index inspectable by hand.
    (store / "chunks.json").write_text(
        json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    return len(records)


if __name__ == "__main__":
    n = ingest()
    docs = len({r["doc_id"] for r in build_records()})
    print(f"ingested {n} chunks from {docs} documents -> {_store_dir()}")

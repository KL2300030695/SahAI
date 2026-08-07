"""
Hybrid retriever: local embeddings (Chroma/ONNX MiniLM) + BM25, fused.

Two design points worth stating explicitly, because both are scored dimensions:

1. **This is not an LLM call.** Retrieval is tier NONE -- zero tokens, zero
   dollars, single-digit milliseconds. It is the highest-frequency step in the
   pipeline and it runs entirely on local compute. That is the concrete form of
   the "a smaller model or classical method can replace constant LLM calls"
   principle.

2. **Stale chunks are dropped here, not judged later.** A chunk whose
   `effective_to` has passed never reaches the reasoning model at all. The
   guardrail's `no_stale_terms` check is the second line of defence; this is the
   first. The seed KB deliberately contains an expired 2024 fee schedule so this
   path is exercised rather than assumed.

Why hybrid: embeddings alone are weak on exact tokens like "₹250", "PAN", or
"one ninety nine" -- precisely the terms a fintech agent must quote correctly.
BM25 catches those. Vectors catch paraphrase ("what's the catch" -> hidden
charges). Reciprocal rank fusion needs no score normalisation between the two.
"""

from __future__ import annotations

import json
import re
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from app.config import BACKEND_DIR, get_settings
from app.rag.ingest import COLLECTION
from app.schemas import Citation, Intent, RetrievalIn, RetrievalOut

RRF_K = 60  # standard reciprocal-rank-fusion damping constant

_TOKEN = re.compile(r"[a-z0-9₹%]+")


def _tokenise(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def _store_dir() -> Path:
    s = get_settings()
    p = Path(s.chroma_dir)
    return p if p.is_absolute() else (BACKEND_DIR / p).resolve()


def _parse_date(value: str) -> Optional[date]:
    try:
        return date.fromisoformat(value.strip())
    except Exception:
        return None


def is_stale(meta: dict[str, Any], on: Optional[date] = None) -> bool:
    """True if the chunk's validity window has closed."""
    on = on or date.today()
    eff_to = _parse_date(str(meta.get("effective_to") or ""))
    if eff_to and eff_to < on:
        return True
    return str(meta.get("status", "")).lower() == "superseded"


class HybridRetriever:
    def __init__(self) -> None:
        store = _store_dir()
        chunks_file = store / "chunks.json"
        if not chunks_file.exists():
            raise RuntimeError(
                f"KB index not found at {store}. Run: python -m app.rag.ingest"
            )

        self.records: list[dict[str, Any]] = json.loads(
            chunks_file.read_text(encoding="utf-8")
        )
        self.by_id = {r["chunk_id"]: r for r in self.records}

        import chromadb
        from rank_bm25 import BM25Okapi

        self._client = chromadb.PersistentClient(path=str(store))
        self._collection = self._client.get_collection(COLLECTION)

        self._bm25_ids = [r["chunk_id"] for r in self.records]
        self._bm25 = BM25Okapi([_tokenise(r["text"]) for r in self.records])

    # -- ranking ---------------------------------------------------------

    def _vector_ranking(self, query: str, n: int) -> list[str]:
        res = self._collection.query(query_texts=[query], n_results=n)
        ids = res.get("ids") or [[]]
        return list(ids[0]) if ids else []

    def _bm25_ranking(self, query: str, n: int) -> list[str]:
        scores = self._bm25.get_scores(_tokenise(query))
        ranked = sorted(
            range(len(scores)), key=lambda i: float(scores[i]), reverse=True
        )
        return [self._bm25_ids[i] for i in ranked[:n] if float(scores[i]) > 0.0]

    def _fuse(self, rankings: list[list[str]]) -> list[tuple[str, float]]:
        """Reciprocal rank fusion. Avoids normalising cosine against BM25."""
        fused: dict[str, float] = {}
        for ranking in rankings:
            for rank, chunk_id in enumerate(ranking):
                fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank + 1)
        return sorted(fused.items(), key=lambda kv: kv[1], reverse=True)

    # -- public ----------------------------------------------------------

    def search(
        self, query: str, k: int = 4, pool: int = 12
    ) -> tuple[list[Citation], list[str]]:
        """Returns (citations, dropped_stale_chunk_ids)."""
        fused = self._fuse(
            [self._vector_ranking(query, pool), self._bm25_ranking(query, pool)]
        )

        citations: list[Citation] = []
        dropped: list[str] = []
        for chunk_id, score in fused:
            rec = self.by_id.get(chunk_id)
            if not rec:
                continue
            if is_stale(rec):
                dropped.append(chunk_id)
                continue
            citations.append(
                Citation(
                    doc_id=rec["doc_id"],
                    title=rec["title"],
                    chunk_id=chunk_id,
                    text=rec["text"],
                    score=round(score, 5),
                    version=rec.get("version", "v1"),
                    effective_from=rec.get("effective_from") or None,
                    effective_to=rec.get("effective_to") or None,
                    source_path=rec.get("source_path"),
                )
            )
            if len(citations) >= k:
                break

        return citations, dropped


@lru_cache
def get_retriever() -> HybridRetriever:
    return HybridRetriever()


# ---------------------------------------------------------------------------
# Query construction
# ---------------------------------------------------------------------------

# Intent-conditioned hint terms. Steers retrieval toward the right corner of the
# KB without an LLM call to rewrite the query.
_INTENT_HINTS: dict[Intent, str] = {
    Intent.PRICING: "fee interest charges zero cost instalment total repaid",
    Intent.ELIGIBILITY: "eligibility criteria approved limit underwriting",
    Intent.KYC_STEPS: "KYC steps Aadhaar OTP PAN mandate documents",
    Intent.OBJECTION_COST: "hidden charges catch processing fee late fee business model",
    Intent.OBJECTION_TRUST: "credit score bureau enquiry privacy Aadhaar data safety scam",
    Intent.DROPOFF_RISK: "drop-off resume saved progress follow-up",
    Intent.READY_TO_CONVERT: "KYC steps mandate schedule dates",
    Intent.COMPLAINT: "complaint escalation supervisor route stop selling merchant "
    "dispute unresolved",
    Intent.PAYMENT_ISSUE: "debit failed bounce charged twice refund late fee "
    "dispute collections",
    Intent.SMALLTALK: "",
    Intent.OTHER: "",
}


def build_query(
    utterance: str, intent: Intent, entities: dict[str, str] | None = None
) -> str:
    parts = [utterance.strip()]
    hint = _INTENT_HINTS.get(intent, "")
    if hint:
        parts.append(hint)
    for key in ("product", "merchant", "city"):
        if entities and entities.get(key):
            parts.append(str(entities[key]))
    return " ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Agent-facing entry point
# ---------------------------------------------------------------------------


def retrieve(inp: RetrievalIn) -> RetrievalOut:
    citations, dropped = get_retriever().search(inp.query, k=inp.k)
    return RetrievalOut(
        query=inp.query, citations=citations, facts=[], dropped_stale=dropped
    )

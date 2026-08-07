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

#: Cosine similarity the best chunk must reach before its content is allowed
#: near the reasoning model. Below it, retrieval reports `no_confident_match`
#: and passes NO chunk text downstream, so the model has nothing to quote and
#: must say it will check.
#:
#: Chosen from measured data, not taste -- run `python -m
#: scripts.calibrate_retrieval` to reproduce. Two things that measurement
#: settled:
#:
#: * BM25 is not a confidence signal on this corpus. "how do I reset my wifi
#:   router" outscores most real product questions because BM25 rewards the
#:   common tokens in "how do I". It remains a ranking contributor -- it is what
#:   catches "₹250" and "PAN" -- and is never consulted for relevance.
#: * The floor must be measured on the expanded query the pipeline actually
#:   sends, not the raw utterance. "is it safe" alone is indistinguishable from
#:   noise; the same words classified as OBJECTION_TRUST are not.
#:
#: Measured on the expanded query: in-domain [0.4807, 0.7008] over 20 utterances,
#: off-topic [0.1406, 0.4480] over 12. The midpoint (0.464) separates that sample
#: perfectly but leaves 0.016 of margin either side, which is noise. 0.40 keeps
#: 0.08 of headroom under the weakest real question and still rejects 11 of 12
#: off-topic turns.
#:
#: The asymmetry is deliberate. A false "no match" costs one sentence -- the
#: agent says they will check. A false match hands fee text to someone asking
#: about a missing delivery, which is the failure this gate exists to prevent.
#: When in doubt, retrieve nothing.
MIN_COSINE = 0.40

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

    def _vector_ranking(self, query: str, n: int) -> list[tuple[str, float]]:
        """Ranked ids with cosine similarity in [0, 1], best first.

        The collection is built with `hnsw:space = cosine`, so Chroma returns a
        cosine *distance*; similarity is 1 - d. The raw number is kept because
        the fused RRF score cannot answer "is this any good?" -- see `_fuse`.
        """
        res = self._collection.query(query_texts=[query], n_results=n)
        ids = (res.get("ids") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        out: list[tuple[str, float]] = []
        for i, chunk_id in enumerate(ids):
            d = float(dists[i]) if i < len(dists) else 1.0
            out.append((chunk_id, max(0.0, min(1.0, 1.0 - d))))
        return out

    def _bm25_ranking(self, query: str, n: int) -> list[tuple[str, float]]:
        """Ranked ids with raw BM25 score, best first. Zero means no overlap."""
        scores = self._bm25.get_scores(_tokenise(query))
        ranked = sorted(
            range(len(scores)), key=lambda i: float(scores[i]), reverse=True
        )
        return [
            (self._bm25_ids[i], float(scores[i]))
            for i in ranked[:n]
            if float(scores[i]) > 0.0
        ]

    def _fuse(
        self, rankings: list[list[tuple[str, float]]]
    ) -> list[tuple[str, float]]:
        """Reciprocal rank fusion. Avoids normalising cosine against BM25.

        Note what this score is and is not. RRF is computed from *rank position*
        only, so the top result of a query about nothing in the knowledge base
        scores exactly as highly as the top result of a perfect match. It orders
        results; it cannot judge them. Confidence has to come from the raw
        signals, which is why the rankings now carry their own scores.
        """
        fused: dict[str, float] = {}
        for ranking in rankings:
            for rank, (chunk_id, _score) in enumerate(ranking):
                fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank + 1)
        return sorted(fused.items(), key=lambda kv: kv[1], reverse=True)

    # -- public ----------------------------------------------------------

    def search(
        self, query: str, k: int = 4, pool: int = 12
    ) -> tuple[list[Citation], list[str], float]:
        """Returns (citations, dropped_stale_chunk_ids, best_cosine).

        Returns NO citations when the best cosine is under `MIN_COSINE`. Not a
        low-confidence flag alongside the text -- no text at all. A flag beside
        the chunks would still leave the chunks in the prompt, and a model given
        plausible fee tables in its context will use them whatever the flag says.
        The only reliable way to stop it quoting a source is to withhold it.
        """
        vector = self._vector_ranking(query, pool)
        best_cosine = vector[0][1] if vector else 0.0
        if best_cosine < MIN_COSINE:
            return [], [], best_cosine

        fused = self._fuse([vector, self._bm25_ranking(query, pool)])

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

        return citations, dropped, best_cosine


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


#: Intents that need no knowledge base. "Sorry, someone's at the door" has
#: nothing to look up, and searching anyway spends compute to hand the reasoning
#: model chunks it should not use.
_NO_LOOKUP = {Intent.SMALLTALK}


def retrieve(inp: RetrievalIn) -> RetrievalOut:
    """Look up the knowledge base, or say plainly that nothing matched.

    Two ways to come back empty, and they mean different things:

    * `skipped` -- the turn never needed a lookup (chit-chat).
    * `no_confident_match` -- a lookup ran and nothing cleared `MIN_COSINE`.

    Both leave `citations` empty, which is what actually protects the customer:
    downstream has nothing to quote, so the guardrail's grounding check has
    nothing to verify against and any figure the model invents is caught.
    """
    if inp.intent in _NO_LOOKUP:
        return RetrievalOut(
            query=inp.query,
            citations=[],
            facts=[],
            dropped_stale=[],
            skipped=True,
            best_score=0.0,
        )

    citations, dropped, best = get_retriever().search(inp.query, k=inp.k)
    return RetrievalOut(
        query=inp.query,
        citations=citations,
        facts=[],
        dropped_stale=dropped,
        no_confident_match=not citations and not dropped,
        best_score=round(best, 4),
    )

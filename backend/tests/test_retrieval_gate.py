"""
The retrieval confidence floor.

The rule is that nothing unsourced reaches the reasoning model. Before this
existed, reciprocal rank fusion always returned four confident-looking chunks
with real ids no matter what was asked, because RRF scores rank position rather
than relevance -- the top hit for "how do I reset my wifi router" scored exactly
as highly as the top hit for "what is the late fee".

Thresholds are measured, not guessed: `python -m scripts.calibrate_retrieval`.
"""

from __future__ import annotations

import pytest

from app.rag.retriever import MIN_COSINE, build_query, get_retriever, retrieve
from app.schemas import Intent, RetrievalIn


def _out(utterance: str, intent: Intent):
    return retrieve(
        RetrievalIn(
            query=build_query(utterance, intent, None), intent=intent, k=4
        )
    )


@pytest.mark.parametrize(
    "utterance,intent",
    [
        ("what is the late fee", Intent.PRICING),
        ("how do I complete KYC", Intent.KYC_STEPS),
        ("is there any hidden charge", Intent.OBJECTION_COST),
        ("will it affect my cibil", Intent.OBJECTION_TRUST),
        ("what's the catch", Intent.OBJECTION_COST),
    ],
)
def test_real_questions_still_retrieve(utterance, intent):
    """The floor must not become a wall -- these are the core demo moments."""
    out = _out(utterance, intent)
    assert out.citations, f"{utterance!r} scored {out.best_score}"
    assert not out.no_confident_match
    assert out.best_score >= MIN_COSINE


@pytest.mark.parametrize(
    "utterance",
    [
        "what is the weather in Chennai tomorrow",
        "my air conditioner is making a rattling noise",
        "how do I reset my wifi router",
        "who won the cricket match last night",
    ],
)
def test_off_topic_returns_no_source_at_all(utterance):
    """Not a low-confidence flag beside the chunks -- no chunks.

    A model handed plausible fee tables will quote them whatever flag sits
    alongside. Withholding the text is the only thing that actually works.
    """
    out = _out(utterance, Intent.OTHER)
    assert out.no_confident_match
    assert out.citations == []
    assert out.best_score < MIN_COSINE


def test_chit_chat_skips_the_lookup_entirely():
    out = _out("how is your day going", Intent.SMALLTALK)
    assert out.skipped
    assert not out.no_confident_match  # a different thing from finding nothing
    assert out.citations == []


def test_the_two_empty_cases_are_distinguishable():
    """`skipped` and `no_confident_match` both give no citations for different
    reasons, and only one of them warrants promising to check and come back."""
    chit = _out("how is your day going", Intent.SMALLTALK)
    miss = _out("how do I reset my wifi router", Intent.OTHER)
    assert (chit.skipped, chit.no_confident_match) == (True, False)
    assert (miss.skipped, miss.no_confident_match) == (False, True)


def test_bm25_is_not_used_as_a_confidence_signal():
    """The measurement that shaped the design, pinned as a test.

    "how do I reset my wifi router" outscores real product questions on BM25
    because BM25 rewards the common tokens in "how do I". If someone later wires
    BM25 into the confidence decision, this fails.
    """
    r = get_retriever()
    off = r._bm25_ranking(build_query("how do I reset my wifi router", Intent.OTHER, None), 12)
    real = r._bm25_ranking(build_query("how long does approval take", Intent.ELIGIBILITY, None), 12)
    assert off and real
    # BM25 genuinely rates the off-topic query competitively...
    assert off[0][1] > 5.0
    # ...yet it is rejected, because only cosine is consulted.
    assert _out("how do I reset my wifi router", Intent.OTHER).no_confident_match

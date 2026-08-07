"""
Measure retrieval score distributions so the confidence floor is chosen from
data rather than invented.

Run:  python -m scripts.calibrate_retrieval

Two findings drove the final design, and both are visible in the output:

1. **BM25 cannot judge relevance on this corpus.** "how do I reset my wifi
   router" outscores most genuine product questions, because BM25 rewards the
   common tokens in "how do I". It stays a ranking contributor -- it is what
   catches "₹250" and "PAN" -- but it is never consulted for confidence.

2. **The floor has to be measured on the query the pipeline actually sends.**
   Retrieval does not see the raw utterance; it sees `build_query(utterance,
   intent, entities)`, which appends intent-specific expansion terms. Measuring
   raw text understates in-domain scores badly -- "is it safe" alone is nearly
   indistinguishable from off-topic noise, while the same utterance classified
   as OBJECTION_TRUST is not.

Intents here are hand-labelled on purpose: this calibrates the retrieval floor
in isolation, so a classifier mistake does not silently move the threshold.
"""

from __future__ import annotations

from app.rag.retriever import build_query, get_retriever
from app.schemas import Intent

# (utterance, intent the classifier should produce)
IN_DOMAIN: list[tuple[str, Intent]] = [
    ("what happens if I miss a payment", Intent.PRICING),
    ("is there any hidden charge", Intent.OBJECTION_COST),
    ("how do I complete KYC", Intent.KYC_STEPS),
    ("what documents do I need", Intent.KYC_STEPS),
    ("is this really zero cost", Intent.OBJECTION_COST),
    ("what is the late fee", Intent.PRICING),
    ("do you check my credit score", Intent.OBJECTION_TRUST),
    ("how long does approval take", Intent.ELIGIBILITY),
    ("can I pay it off early", Intent.PRICING),
    ("what is pay in 3", Intent.PRICING),
    # short and elliptical -- how people actually speak on a call
    ("any charges", Intent.OBJECTION_COST),
    ("how much", Intent.PRICING),
    ("is it safe", Intent.OBJECTION_TRUST),
    ("aadhaar needed", Intent.KYC_STEPS),
    ("three months right", Intent.PRICING),
    ("what's the catch", Intent.OBJECTION_COST),
    ("bounce fee", Intent.PRICING),
    ("will it affect my cibil", Intent.OBJECTION_TRUST),
    ("no cost emi", Intent.PRICING),
    ("pan card", Intent.KYC_STEPS),
]

# Off-topic turns classify as OTHER or SMALLTALK, which carry no expansion.
OUT_OF_DOMAIN: list[tuple[str, Intent]] = [
    ("what is the weather in Chennai tomorrow", Intent.OTHER),
    ("my air conditioner is making a rattling noise", Intent.OTHER),
    ("can you recommend a good biryani place", Intent.SMALLTALK),
    ("who won the cricket match last night", Intent.SMALLTALK),
    ("my flight to Dubai got cancelled", Intent.OTHER),
    ("how do I reset my wifi router", Intent.OTHER),
    ("how do I change my email password", Intent.OTHER),
    ("what time does the shop close", Intent.OTHER),
    ("my order never arrived from the online store", Intent.OTHER),
    ("can you help me book a cab", Intent.SMALLTALK),
    ("how is your day going", Intent.SMALLTALK),
    ("sorry one second, someone's at the door", Intent.SMALLTALK),
]


def probe(r, utterance: str, intent: Intent) -> tuple[float, float]:
    q = build_query(utterance, intent, None)
    vec = r._vector_ranking(q, 12)
    bm = r._bm25_ranking(q, 12)
    return (vec[0][1] if vec else 0.0, bm[0][1] if bm else 0.0)


def main() -> None:
    r = get_retriever()
    rows: list[tuple[str, str, float, float]] = []
    for q, i in IN_DOMAIN:
        c, b = probe(r, q, i)
        rows.append(("in ", q, c, b))
    for q, i in OUT_OF_DOMAIN:
        c, b = probe(r, q, i)
        rows.append(("out", q, c, b))

    print(f"{'':4}{'utterance':<46}{'cosine':>9}{'bm25':>9}")
    print("-" * 68)
    for kind, q, c, b in rows:
        print(f"{kind:4}{q[:44]:<46}{c:>9.4f}{b:>9.3f}")

    ins = [(c, b) for k, _, c, b in rows if k == "in "]
    outs = [(c, b) for k, _, c, b in rows if k == "out"]

    lo_in, hi_out = min(c for c, _ in ins), max(c for c, _ in outs)
    print("\n--- cosine (the confidence signal) ---")
    print(f"  in-domain  min {lo_in:.4f}   max {max(c for c, _ in ins):.4f}")
    print(f"  off-topic  min {min(c for c, _ in outs):.4f}   max {hi_out:.4f}")
    print(f"  separation {lo_in - hi_out:+.4f}   midpoint {(lo_in + hi_out) / 2:.4f}")

    lo_b, hi_b = min(b for _, b in ins), max(b for _, b in outs)
    print("\n--- bm25 (ranking only -- NOT used for confidence) ---")
    print(f"  in-domain  min {lo_b:.3f}    off-topic max {hi_b:.3f}")
    print(f"  separation {lo_b - hi_b:+.3f}")

    from app.rag.retriever import MIN_COSINE

    fp = [q for k, q, c, _ in rows if k == "out" and c >= MIN_COSINE]
    fn = [q for k, q, c, _ in rows if k == "in " and c < MIN_COSINE]
    print(f"\n--- at MIN_COSINE = {MIN_COSINE} ---")
    print(f"  off-topic that would still retrieve ({len(fp)}/{len(outs)}): {fp}")
    print(f"  in-domain that would be flagged no-match ({len(fn)}/{len(ins)}): {fn}")


if __name__ == "__main__":
    main()

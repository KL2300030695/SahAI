"""
Deterministic guardrail checks.

Five of the seven checks in the system live here, and every one is ordinary
Python. That is the design claim: the guardrails that matter most are not
instructions a model is asked to follow, they are conditions the code enforces
before an output can reach a customer.

Each function returns a CheckResult carrying `enforced_by="code"`, which the
dashboard renders differently from the model-judged checks. A reviewer can see
at a glance which parts of the safety story survive an adversarial customer.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Iterable, Optional

from app.config import CREDIT_TERM_PATTERNS
from app.guardrails.pii import scan
from app.rag.retriever import is_stale
from app.schemas import (
    ActionType,
    CheckName,
    CheckResult,
    Citation,
    GroundedSpan,
    Severity,
)

_CREDIT_RE = re.compile("|".join(CREDIT_TERM_PATTERNS), re.IGNORECASE)

# Claims that must be traceable to a retrieved chunk: money amounts, percentages,
# day/month counts, and bare multi-digit numbers.
_MONEY = re.compile(r"₹\s?[\d,]+(?:\.\d+)?|\brs\.?\s?[\d,]+", re.IGNORECASE)
_PERCENT = re.compile(r"\b\d+(?:\.\d+)?\s?%")
_DURATION = re.compile(
    r"\b\d+\s?(?:day|days|month|months|week|weeks|year|years|hour|hours|minute|minutes)\b",
    re.IGNORECASE,
)
# Comma grouping has to be part of the pattern, not left to the tokeniser. An
# earlier version matched "60,000" as the bare number "000" -- it started after
# the comma, because a comma was not in the lookbehind. That made a correctly
# sourced figure look ungrounded and blocked a good suggestion.
#
# The group alternative accepts both Western (60,000) and Indian (1,50,000)
# digit grouping.
_BARE_NUMBER = re.compile(
    r"(?<![\w#/.,-])(?:\d{1,3}(?:,\d{2,3})+|\d{3,})(?![\w/.,-]*\d)"
)

# Numbers that are safe without a citation: they are structural to the product
# name, not quotable terms.
_ALLOWED_NUMERIC_LITERALS = {"3", "three", "1", "2"}


def _numeric_spans(text: str) -> list[tuple[str, int, int]]:
    """Every quotable figure with its position, longest match winning.

    Positions matter now: the interface marks sourced figures inside the
    sentence, so it needs offsets, not just values. Overlaps are resolved
    longest-first so "₹12,000" is one span rather than a money match sitting on
    top of a bare-number match.
    """
    found: list[tuple[str, int, int]] = []
    for pattern in (_MONEY, _PERCENT, _DURATION, _BARE_NUMBER):
        for m in pattern.finditer(text or ""):
            if m.group(0).strip().lower() in _ALLOWED_NUMERIC_LITERALS:
                continue
            found.append((m.group(0).strip(), m.start(), m.end()))

    found.sort(key=lambda s: (s[1] - s[2], s[1]))  # longest first, then position
    kept: list[tuple[str, int, int]] = []
    for span in found:
        if not any(span[1] < k[2] and k[1] < span[2] for k in kept):
            kept.append(span)
    return sorted(kept, key=lambda s: s[1])


def _numeric_claims(text: str) -> list[str]:
    return [t for t, _, _ in _numeric_spans(text)]


def _normalise_number(s: str) -> str:
    return re.sub(r"[^\d]", "", s or "")


def ground_figures(
    say: str, cited_chunk_ids: Iterable[str], citations: list[Citation]
) -> tuple[list[GroundedSpan], list[str]]:
    """Map each figure in `say` to the cited chunk that contains it.

    Returns (grounded spans, ungrounded figure text). This is the single source
    of truth for both the guardrail verdict and the interface's inline marks, so
    the two can never disagree about what is sourced.
    """
    by_id = {c.chunk_id: c for c in citations}
    cited = [cid for cid in (cited_chunk_ids or []) if cid in by_id]

    # Which numbers each cited chunk actually contains.
    chunk_numbers: dict[str, set[str]] = {
        cid: {
            _normalise_number(n)
            for n in _numeric_claims(by_id[cid].text)
            if _normalise_number(n)
        }
        for cid in cited
    }

    grounded: list[GroundedSpan] = []
    ungrounded: list[str] = []
    for text, start, end in _numeric_spans(say):
        digits = _normalise_number(text)
        if not digits:
            continue
        source = next((cid for cid in cited if digits in chunk_numbers[cid]), None)
        if source is None:
            ungrounded.append(text)
            continue
        c = by_id[source]
        grounded.append(
            GroundedSpan(
                text=text,
                start=start,
                end=end,
                chunk_id=source,
                doc_title=c.title,
                version=c.version,
            )
        )
    return grounded, ungrounded


# ---------------------------------------------------------------------------
# 1. consent
# ---------------------------------------------------------------------------


def check_consent(consent_ack: bool) -> CheckResult:
    return CheckResult(
        name=CheckName.CONSENT_RECORDED,
        passed=bool(consent_ack),
        detail=(
            "Consent to recording and AI assistance is on record for this call."
            if consent_ack
            else "No consent on record. The orchestrator will not process turns "
            "for this call until consent is captured."
        ),
        enforced_by="code",
        severity=Severity.BLOCK,
    )


# ---------------------------------------------------------------------------
# 2. grounding
# ---------------------------------------------------------------------------


def check_grounding(
    say: str, cited_chunk_ids: Iterable[str], citations: list[Citation]
) -> CheckResult:
    """Every quotable number in the suggestion must appear in a cited chunk.

    This is the check that makes "never quote stale or incorrect terms" real. A
    model that invents ₹499 gets blocked, because ₹499 is not in the retrieved
    text — no judgement call, no second model, just set membership.
    """
    cited = set(cited_chunk_ids or [])
    claims = _numeric_claims(say)

    if not claims:
        return CheckResult(
            name=CheckName.GROUNDING,
            passed=True,
            detail="No quotable figures in the suggestion; nothing to ground.",
            enforced_by="code",
            severity=Severity.BLOCK,
        )

    if not cited:
        return CheckResult(
            name=CheckName.GROUNDING,
            passed=False,
            detail=(
                f"Suggestion states {len(claims)} figure(s) ({', '.join(claims[:4])}) "
                "but cites no knowledge-base chunk. Untraceable claims are blocked."
            ),
            enforced_by="code",
            severity=Severity.BLOCK,
        )

    grounded, ungrounded = ground_figures(say, cited, citations)
    if ungrounded:
        return CheckResult(
            name=CheckName.GROUNDING,
            passed=False,
            detail=(
                f"Figure(s) not present in any cited chunk: {', '.join(ungrounded[:4])}. "
                f"Cited: {', '.join(sorted(cited))}."
            ),
            enforced_by="code",
            severity=Severity.BLOCK,
        )

    return CheckResult(
        name=CheckName.GROUNDING,
        passed=True,
        detail=(
            f"All {len(grounded)} figure(s) traced to cited chunk(s): "
            f"{', '.join(sorted({g.chunk_id for g in grounded}))}."
        ),
        enforced_by="code",
        severity=Severity.BLOCK,
    )


# ---------------------------------------------------------------------------
# 3. human oversight on credit terms
# ---------------------------------------------------------------------------


def mentions_credit_terms(text: str) -> bool:
    return bool(_CREDIT_RE.search(text or ""))


def check_credit_terms(
    say: str, action_type: ActionType, requires_human_confirmation: bool
) -> tuple[CheckResult, bool]:
    """Returns (result, forced_flag).

    `forced_flag` is what the orchestrator writes back onto the suggestion. The
    model's own value is treated as advisory and can only ever be raised, never
    lowered — so the AI cannot finalise a credit term even if it decides it
    should.
    """
    triggered = action_type == ActionType.QUOTE_TERMS or mentions_credit_terms(say)
    forced = bool(requires_human_confirmation or triggered)

    if triggered and not requires_human_confirmation:
        return (
            CheckResult(
                name=CheckName.NO_AUTONOMOUS_CREDIT_TERMS,
                passed=True,
                detail=(
                    "Output touches regulated credit terminology and the model did "
                    "not flag it. Human-confirmation flag FORCED on by code."
                ),
                enforced_by="code",
                severity=Severity.WARN,
            ),
            True,
        )

    return (
        CheckResult(
            name=CheckName.NO_AUTONOMOUS_CREDIT_TERMS,
            passed=True,
            detail=(
                "Credit-sensitive content — human confirmation required before "
                "this is said to the customer."
                if forced
                else "No regulated credit terminology in this suggestion."
            ),
            enforced_by="code",
            severity=Severity.WARN,
        ),
        forced,
    )


# ---------------------------------------------------------------------------
# 4. PII
# ---------------------------------------------------------------------------


def check_pii(text: str, redacted: Optional[str]) -> CheckResult:
    remaining = scan(redacted if redacted is not None else text)
    original = scan(text)
    if remaining:
        return CheckResult(
            name=CheckName.PII_REDACTION,
            passed=False,
            detail=f"PII still present after redaction: {', '.join(remaining)}.",
            enforced_by="code",
            severity=Severity.BLOCK,
        )
    return CheckResult(
        name=CheckName.PII_REDACTION,
        passed=True,
        detail=(
            f"Redacted before display and storage: {', '.join(original)}."
            if original
            else "No PII detected."
        ),
        enforced_by="code",
        severity=Severity.BLOCK,
    )


# ---------------------------------------------------------------------------
# 5. staleness
# ---------------------------------------------------------------------------


def check_stale_terms(
    citations: list[Citation],
    cited_chunk_ids: Iterable[str],
    dropped_stale: Optional[list[str]] = None,
    on: Optional[date] = None,
) -> CheckResult:
    """Fails if the suggestion cites a chunk outside its validity window.

    The retriever already drops expired chunks, so in normal operation this
    passes. It exists as the second line of defence for the case where a chunk
    expires between index build and answer time.
    """
    cited = set(cited_chunk_ids or [])
    by_id = {c.chunk_id: c for c in citations}
    stale_cited = [
        cid
        for cid in cited
        if cid in by_id
        and is_stale(
            {
                "effective_to": by_id[cid].effective_to or "",
                "status": "superseded" if by_id[cid].effective_to else "active",
            },
            on,
        )
    ]

    if stale_cited:
        return CheckResult(
            name=CheckName.NO_STALE_TERMS,
            passed=False,
            detail=f"Cites expired knowledge-base chunk(s): {', '.join(stale_cited)}.",
            enforced_by="code",
            severity=Severity.BLOCK,
        )

    note = ""
    if dropped_stale:
        note = (
            f" Retriever filtered {len(dropped_stale)} expired chunk(s) before "
            f"the model saw them: {', '.join(dropped_stale[:3])}."
        )
    return CheckResult(
        name=CheckName.NO_STALE_TERMS,
        passed=True,
        detail=f"All cited terms are within their validity window.{note}",
        enforced_by="code",
        severity=Severity.BLOCK,
    )


# ---------------------------------------------------------------------------
# 6. injection screen (result of the TINY model, recorded as a check)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 7. no fabricated completed actions
# ---------------------------------------------------------------------------

# The assistant has no side effects: it cannot send email or SMS, look anything
# up, or alter an account. A suggestion asserting it already did is worse than
# a wrong fee -- the human agent reads it aloud, the customer waits for an email
# that never arrives, and trust is gone.
#
# Observed live: "Sure Arun, I've just sent you an email with all the Pay-in-3
# details and your account info. Please check your inbox."
#
# The prompt now forbids this, but a prompt is a request. This is the check.
_COMPLETION_VERBS = (
    "sent|emailed|texted|messaged|shared|updated|processed|submitted|applied|"
    "activated|approved|booked|added|scheduled|arranged|"
    # Found by watching a live run: the model wrote "I've marked you as
    # do-not-call" while the CRM patch was still pending_agent_approval.
    # Record-keeping verbs are exactly as much of a false promise as "sent" --
    # the customer believes something is on file when nothing has been written.
    "marked|noted|recorded|flagged|logged|registered|removed|cancelled|"
    "canceled|deleted|disabled|enabled|set\\s+up|opted\\s+you\\s+out"
)

_COMPLETED_ACTION = re.compile(
    r"\b(?:"
    rf"i(?:'ve| have)\s+(?:just\s+|already\s+)?(?:{_COMPLETION_VERBS})"
    rf"|we(?:'ve| have)\s+(?:just\s+|already\s+)?(?:{_COMPLETION_VERBS})"
    r"|(?:i|we)\s+(?:just\s+)?sent\s+you"
    rf"|(?:has|have)\s+been\s+(?:{_COMPLETION_VERBS})"
    r"|check\s+your\s+(?:inbox|email|messages|sms)"
    r"|it(?:'s| is)\s+(?:on\s+its\s+way|been\s+sent|done)"
    r"|you(?:'re| are)\s+(?:now\s+)?(?:all\s+set|signed\s+up|registered)"
    r")\b",
    re.IGNORECASE,
)


def check_no_fabricated_actions(say: str) -> CheckResult:
    """Block a suggestion that claims an action was already carried out."""
    match = _COMPLETED_ACTION.search(say or "")
    if match:
        return CheckResult(
            name=CheckName.NO_FABRICATED_ACTIONS,
            passed=False,
            detail=(
                f"Claims a completed action ({match.group(0)!r}). The assistant "
                "has no side effects — it cannot send, update, or submit "
                "anything. Saying so to a customer is a false promise."
            ),
            enforced_by="code",
            severity=Severity.BLOCK,
        )
    return CheckResult(
        name=CheckName.NO_FABRICATED_ACTIONS,
        passed=True,
        detail="No claim of a completed action.",
        enforced_by="code",
        severity=Severity.BLOCK,
    )


# ---------------------------------------------------------------------------
# 8. injection screen (result of the TINY model, recorded as a check)
# ---------------------------------------------------------------------------


def check_injection(flagged: bool, score: float = 0.0) -> CheckResult:
    return CheckResult(
        name=CheckName.INJECTION_SCREEN,
        passed=not flagged,
        detail=(
            f"Prompt-injection attempt detected (score {score:.4f}). The utterance "
            "was not passed to any reasoning model; the agent was warned instead."
            if flagged
            else f"No manipulation attempt detected (score {score:.4f})."
        ),
        enforced_by="llm",
        severity=Severity.BLOCK if flagged else Severity.INFO,
    )


def check_figures_in_source(text: str, source_text: str) -> CheckResult:
    """Every figure in a summary must appear in the call it summarises.

    The right grounding question for an internal note is not "is this in the
    handbook?" but "was this actually said?". Running the KB check on a summary
    was a guaranteed false positive: post-call retrieval passes no citations, so
    any summary mentioning a number failed, and "1 check stopped this" appeared
    on nearly every call. A guardrail that cries wolf on ordinary output trains
    the agent to stop reading the panel, which costs more than it saves.

    What it does catch is real and worth catching: a summariser inventing a
    figure nobody said -- writing "agreed to a Rs 5,000 limit" into a customer's
    record when no such number appears anywhere in the transcript.
    """
    figures = _numeric_claims(text)
    if not figures:
        return CheckResult(
            name=CheckName.GROUNDING,
            passed=True,
            detail="No figures in the summary; nothing to check.",
            enforced_by="code",
        )

    # Compare figure-to-figure rather than searching one concatenated digit
    # string. Flattening a whole transcript to digits makes "1" followed by "99"
    # satisfy a claim of "199", which would let an invented number through --
    # the one thing this check exists to stop.
    source_figures = {_normalise_number(f) for f in _numeric_claims(source_text or "")}
    raw = (source_text or "").lower()
    missing = [
        f
        for f in figures
        if _normalise_number(f) not in source_figures and f.lower() not in raw
    ]
    if missing:
        return CheckResult(
            name=CheckName.GROUNDING,
            passed=False,
            detail=(
                f"Summary states {len(missing)} figure(s) that appear nowhere in "
                f"the call: {', '.join(missing)}. A record must not contain "
                "numbers nobody said."
            ),
            enforced_by="code",
            severity=Severity.BLOCK,
        )
    return CheckResult(
        name=CheckName.GROUNDING,
        passed=True,
        detail=(
            f"All {len(figures)} figure(s) in the summary appear in the "
            "transcript."
        ),
        enforced_by="code",
    )

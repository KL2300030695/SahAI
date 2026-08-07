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
_BARE_NUMBER = re.compile(r"(?<![\w#/.-])\d{3,}(?![\w/.-])")

# Numbers that are safe without a citation: they are structural to the product
# name, not quotable terms.
_ALLOWED_NUMERIC_LITERALS = {"3", "three", "1", "2"}


def _numeric_claims(text: str) -> list[str]:
    claims: list[str] = []
    for pattern in (_MONEY, _PERCENT, _DURATION, _BARE_NUMBER):
        claims.extend(m.group(0).strip() for m in pattern.finditer(text or ""))
    return [c for c in claims if c.lower() not in _ALLOWED_NUMERIC_LITERALS]


def _normalise_number(s: str) -> str:
    return re.sub(r"[^\d]", "", s or "")


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
    by_id = {c.chunk_id: c for c in citations}
    corpus = " ".join(by_id[cid].text for cid in cited if cid in by_id)
    corpus_digits = {_normalise_number(n) for n in _numeric_claims(corpus)}

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

    ungrounded = [
        c
        for c in claims
        if _normalise_number(c) and _normalise_number(c) not in corpus_digits
    ]
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
            f"All {len(claims)} figure(s) traced to cited chunk(s): "
            f"{', '.join(sorted(cited))}."
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
_COMPLETED_ACTION = re.compile(
    r"\b(?:"
    r"i(?:'ve| have)\s+(?:just\s+)?(?:sent|emailed|texted|messaged|shared|"
    r"updated|processed|submitted|applied|activated|approved|booked|added|"
    r"scheduled|arranged)"
    r"|(?:i|we)\s+(?:just\s+)?sent\s+you"
    r"|(?:has|have)\s+been\s+(?:sent|emailed|updated|processed|activated|"
    r"submitted|approved)"
    r"|check\s+your\s+(?:inbox|email|messages|sms)"
    r"|it(?:'s| is)\s+(?:on\s+its\s+way|been\s+sent)"
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

"""
Cost-tiered model routing.

The escalation rules are ordinary Python predicates, not prompt text. That
matters twice over: a model cannot talk its way into a cheaper tier on a
sensitive turn, and every escalation carries a named trigger string that lands
in the cost ledger and the UI. The cost story is therefore auditable -- you can
point at the row and say which rule fired and what it cost.

Default posture is cheap. Expense is opt-in and must be justified by a rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

from app.config import CREDIT_TERM_PATTERNS
from app.schemas import Intent, ModelTier

_CREDIT_RE = re.compile("|".join(CREDIT_TERM_PATTERNS), re.IGNORECASE)

# Intents where a wrong answer is a compliance problem rather than a bad
# customer experience. These always get the strongest model.
SENSITIVE_INTENTS = {
    Intent.ELIGIBILITY,
    Intent.OBJECTION_TRUST,
    Intent.OBJECTION_COST,
}

DROPOFF_ESCALATION_THRESHOLD = 0.6
LOW_CONFIDENCE_THRESHOLD = 0.6


@dataclass(frozen=True)
class EscalationRule:
    name: str
    why: str
    predicate: Callable[["RouteContext"], bool]


@dataclass
class RouteContext:
    intent: Intent
    confidence: float
    dropoff_risk: float
    text: str = ""
    agent_requested: bool = False


def mentions_credit_terms(text: str) -> bool:
    """Regex, deliberately. Used both for routing and, in guardrails/rules.py,
    for forcing the human-confirmation flag."""
    return bool(_CREDIT_RE.search(text or ""))


ESCALATION_RULES: list[EscalationRule] = [
    EscalationRule(
        name="sensitive_intent",
        why="Intent touches eligibility, cost, or trust — a wrong answer here is a "
        "compliance issue, not just a lost sale.",
        predicate=lambda c: c.intent in SENSITIVE_INTENTS,
    ),
    EscalationRule(
        name="credit_terms_in_context",
        why="Conversation mentions regulated credit terminology (rate, limit, "
        "approval, tenure, fees).",
        predicate=lambda c: mentions_credit_terms(c.text),
    ),
    EscalationRule(
        name="high_dropoff_risk",
        why=f"Drop-off risk above {DROPOFF_ESCALATION_THRESHOLD:.2f} — this is the "
        "turn where the call is won or lost.",
        predicate=lambda c: c.dropoff_risk > DROPOFF_ESCALATION_THRESHOLD,
    ),
    EscalationRule(
        name="low_intent_confidence",
        why=f"Intent confidence below {LOW_CONFIDENCE_THRESHOLD:.2f} — the cheap "
        "classifier is unsure, so do not build a suggestion on it.",
        predicate=lambda c: c.confidence < LOW_CONFIDENCE_THRESHOLD,
    ),
    EscalationRule(
        name="agent_requested",
        why="The human agent explicitly asked for a second opinion.",
        predicate=lambda c: c.agent_requested,
    ),
]


def route_nba(ctx: RouteContext) -> tuple[ModelTier, Optional[str]]:
    """Pick the tier for a next-best-action decision.

    Returns (tier, trigger). `trigger` is None when the cheap path was taken.
    """
    fired = [r for r in ESCALATION_RULES if r.predicate(ctx)]
    if fired:
        return ModelTier.HIGH, "; ".join(f"{r.name}: {r.why}" for r in fired)
    return ModelTier.STANDARD, None


def route_selfcheck(escalated: bool, stage: str = "live_turn") -> ModelTier:
    """Self-check tier.

    Post-call artefacts are customer-facing and CRM-writing, so they always get
    an LLM adjudication pass on top of the code checks. Routine live turns get
    the purpose-built safeguard model; escalated ones get the reasoning model.
    """
    if stage == "post_call":
        return ModelTier.HIGH
    return ModelTier.HIGH if escalated else ModelTier.SAFETY


def describe_routing() -> dict[str, object]:
    """Machine-readable routing policy — surfaced at /api/policy so the tiering
    is inspectable rather than a claim on a slide."""
    return {
        "default_nba_tier": ModelTier.STANDARD.value,
        "escalated_nba_tier": ModelTier.HIGH.value,
        "sensitive_intents": sorted(i.value for i in SENSITIVE_INTENTS),
        "dropoff_threshold": DROPOFF_ESCALATION_THRESHOLD,
        "confidence_threshold": LOW_CONFIDENCE_THRESHOLD,
        "rules": [{"name": r.name, "why": r.why} for r in ESCALATION_RULES],
    }

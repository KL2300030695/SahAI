"""
SahAI agent contracts.

Every agent's input and output type lives here, and nowhere else. Agents import
from this module; they never import each other. That constraint is what makes
this a multi-agent system rather than a mega-prompt with sections -- each agent
can be tested, swapped, or re-tiered in isolation because its only coupling is
to a schema.

Read this file first: the pipeline is legible from the types alone.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Model tiering
# ---------------------------------------------------------------------------


class ModelTier(str, Enum):
    """Cost tier a decision is routed to.

    NONE is a real tier, not a null: retrieval and most guardrail checks are
    deliberately *not* LLM calls. Recording them as NONE lets the cost ledger
    show how much of the pipeline runs at zero marginal token cost.
    """

    NONE = "none"          # local compute only -- $0.00
    TINY = "tiny"          # 86M prompt-guard
    CHEAP = "cheap"        # 8B, every turn
    STANDARD = "standard"  # 20B, routine reasoning
    HIGH = "high"          # 120B, high-stakes reasoning
    SAFETY = "safety"      # 20B safeguard, policy adjudication
    STT = "stt"            # whisper


# ---------------------------------------------------------------------------
# Conversation primitives
# ---------------------------------------------------------------------------


class Speaker(str, Enum):
    AGENT = "agent"
    CUSTOMER = "customer"


class TranscriptTurn(BaseModel):
    index: int
    speaker: Speaker
    text: str
    ts: float = Field(description="Seconds from call start.")


# ---------------------------------------------------------------------------
# 0. InjectionScreen  (tier=TINY)
# ---------------------------------------------------------------------------


class ScreenIn(BaseModel):
    utterance: str


class ScreenOut(BaseModel):
    is_attack: bool = False
    score: float = Field(0.0, ge=0.0, le=1.0)
    detail: str = ""


# ---------------------------------------------------------------------------
# 1. IntentAgent  (tier=CHEAP)
# ---------------------------------------------------------------------------


class Intent(str, Enum):
    PRICING = "pricing"
    ELIGIBILITY = "eligibility"
    KYC_STEPS = "kyc_steps"
    OBJECTION_COST = "objection_cost"
    OBJECTION_TRUST = "objection_trust"
    DROPOFF_RISK = "dropoff_risk"
    READY_TO_CONVERT = "ready_to_convert"
    SMALLTALK = "smalltalk"
    OTHER = "other"


class IntentIn(BaseModel):
    turns: list[TranscriptTurn] = Field(description="Trailing window, newest last.")
    customer_id: Optional[str] = None


class IntentOut(BaseModel):
    intent: Intent = Intent.OTHER
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    entities: dict[str, str] = Field(
        default_factory=dict,
        description="Extracted slots: cart_value, tenure, city, product, ...",
    )
    dropoff_risk: float = Field(
        0.0, ge=0.0, le=1.0, description="Drives post-call follow-up targeting."
    )
    escalate: bool = Field(
        False,
        description="Agent's own signal that this turn is high-stakes. The "
        "orchestrator ORs this with its own code rules -- it is advisory, "
        "never the sole trigger.",
    )
    rationale: str = ""


# ---------------------------------------------------------------------------
# 2. RetrievalAgent  (tier=NONE -- local embeddings + BM25, no LLM call)
# ---------------------------------------------------------------------------


class Citation(BaseModel):
    doc_id: str
    title: str
    chunk_id: str
    text: str
    score: float
    version: str = "v1"
    effective_from: Optional[str] = None
    effective_to: Optional[str] = Field(
        None, description="If set and in the past, the chunk is stale and is dropped."
    )
    source_path: Optional[str] = None


class GroundedFact(BaseModel):
    """A claim the system is willing to put in front of a customer.

    `chunk_id` is mandatory by construction: there is no way to build a
    GroundedFact without naming the chunk it came from. The grounding guardrail
    relies on this -- an ungrounded claim cannot be represented in the type.
    """

    statement: str
    chunk_id: str


class RetrievalIn(BaseModel):
    query: str
    intent: Intent = Intent.OTHER
    entities: dict[str, str] = Field(default_factory=dict)
    k: int = 4


class RetrievalOut(BaseModel):
    query: str
    citations: list[Citation] = Field(default_factory=list)
    facts: list[GroundedFact] = Field(default_factory=list)
    dropped_stale: list[str] = Field(
        default_factory=list, description="chunk_ids filtered for being out of date."
    )


# ---------------------------------------------------------------------------
# 3. NextBestActionAgent  (tier=STANDARD, escalates to HIGH)
# ---------------------------------------------------------------------------


class ActionType(str, Enum):
    EXPLAIN = "explain"
    REASSURE = "reassure"
    QUOTE_TERMS = "quote_terms"        # always requires human confirmation
    SEND_LINK = "send_link"
    ESCALATE_HUMAN = "escalate_human"
    SCHEDULE_FOLLOWUP = "schedule_followup"


class CRMSnapshot(BaseModel):
    customer_id: str
    name: str = ""
    city: str = ""
    kyc_status: str = "not_started"
    credit_limit_inr: Optional[int] = None
    past_interactions: list[str] = Field(default_factory=list)
    last_disposition: Optional[str] = None


class NBAIn(BaseModel):
    intent: Intent
    entities: dict[str, str] = Field(default_factory=dict)
    citations: list[Citation] = Field(default_factory=list)
    crm: Optional[CRMSnapshot] = None
    recent_turns: list[TranscriptTurn] = Field(default_factory=list)
    dropoff_risk: float = 0.0


class NBAOut(BaseModel):
    say: str = Field(description="Verbatim suggestion for the human agent to speak.")
    why: str = Field(description="Rationale surfaced to the agent, not the customer.")
    action_type: ActionType = ActionType.EXPLAIN
    cited_chunk_ids: list[str] = Field(default_factory=list)
    requires_human_confirmation: bool = Field(
        True,
        description="Defaults to True and is FORCED True by guardrails/rules.py for "
        "anything touching loan or credit terms. The model cannot lower it.",
    )


# ---------------------------------------------------------------------------
# 4. CRMFollowUpAgent  (tier=STANDARD, post-call)
# ---------------------------------------------------------------------------


class Disposition(str, Enum):
    CONVERTED = "converted"
    DROPPED = "dropped"
    CALLBACK = "callback"
    NOT_INTERESTED = "not_interested"


class FollowUpDraft(BaseModel):
    channel: Literal["email", "sms"] = "email"
    subject: str = ""
    body: str = ""


class SendStatus(str, Enum):
    """Deliberately not a bool.

    The CRM agent can only ever produce PENDING_AGENT_APPROVAL. Only the
    approval endpoint -- with a human approver id -- can write APPROVED/SENT.
    Human oversight is a state machine here, not a prompt instruction.
    """

    PENDING_AGENT_APPROVAL = "pending_agent_approval"
    APPROVED = "approved"
    SENT = "sent"
    REJECTED = "rejected"


class CRMIn(BaseModel):
    call_id: str
    transcript: list[TranscriptTurn]
    intents_seen: list[Intent] = Field(default_factory=list)
    max_dropoff_risk: float = 0.0
    crm: Optional[CRMSnapshot] = None


class CRMOut(BaseModel):
    summary: str = ""
    crm_patch: dict[str, Any] = Field(
        default_factory=dict,
        description="PROPOSED field diff. Not written until a human approves.",
    )
    disposition: Disposition = Disposition.CALLBACK
    dropoff_reason: Optional[str] = None
    followup_draft: Optional[FollowUpDraft] = None
    send_status: SendStatus = SendStatus.PENDING_AGENT_APPROVAL


# ---------------------------------------------------------------------------
# 5. SelfCheckAgent  (tier=NONE for code checks, SAFETY/HIGH when escalated)
# ---------------------------------------------------------------------------


class CheckName(str, Enum):
    CONSENT_RECORDED = "consent_recorded"
    INJECTION_SCREEN = "injection_screen"
    GROUNDING = "grounding"
    NO_AUTONOMOUS_CREDIT_TERMS = "no_autonomous_credit_terms"
    NO_FABRICATED_ACTIONS = "no_fabricated_actions"
    PII_REDACTION = "pii_redaction"
    NO_STALE_TERMS = "no_stale_terms"
    GOAL_ALIGNMENT = "goal_alignment"


class Severity(str, Enum):
    INFO = "info"
    WARN = "warn"
    BLOCK = "block"


class CheckResult(BaseModel):
    name: CheckName
    passed: bool
    detail: str = ""
    enforced_by: Literal["code", "llm"] = Field(
        description="Shipped to the UI so a reviewer can see which guardrails are "
        "deterministic code (un-promptable) and which are model judgement."
    )
    severity: Severity = Severity.WARN


class CheckIn(BaseModel):
    candidate_say: str
    candidate_why: str = ""
    action_type: ActionType = ActionType.EXPLAIN
    cited_chunk_ids: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    consent_ack: bool = False
    injection_flagged: bool = False
    stage: Literal["live_turn", "post_call"] = "live_turn"
    customer_facing: bool = Field(
        True,
        description="Whether this artefact will be seen by the customer. Internal "
        "artefacts (a CRM summary) still get every code check, but are not "
        "adjudicated against the customer-facing conduct policy -- a note saying "
        "'the customer completed the OTP step' describes an event, it does not "
        "ask anyone for an OTP.",
    )


class CheckOut(BaseModel):
    passed: bool = True
    checks: list[CheckResult] = Field(default_factory=list)
    redacted_say: Optional[str] = None
    blocked_reason: Optional[str] = None

    @property
    def blocking_failures(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed and c.severity == Severity.BLOCK]


# ---------------------------------------------------------------------------
# Cost telemetry
# ---------------------------------------------------------------------------


class DecisionCost(BaseModel):
    """One row in the ledger: one agent decision, its tier, and what it cost."""

    call_id: str
    turn_index: Optional[int] = None
    agent: str
    tier: ModelTier
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    usd: float = 0.0
    latency_ms: float = 0.0
    escalation_trigger: Optional[str] = Field(
        None, description="Why this decision was routed to a more expensive tier."
    )
    at: datetime = Field(default_factory=_utcnow)


class CostLedger(BaseModel):
    call_id: str
    decisions: list[DecisionCost] = Field(default_factory=list)
    total_usd: float = 0.0
    total_inr: float = 0.0
    by_tier_usd: dict[str, float] = Field(default_factory=dict)
    llm_calls: int = 0
    zero_cost_steps: int = Field(
        0, description="Pipeline steps served by local compute instead of an LLM."
    )


# ---------------------------------------------------------------------------
# Orchestrator output -- what the dashboard receives per turn
# ---------------------------------------------------------------------------


class TurnAssist(BaseModel):
    """Everything the agent-assist panel renders for a single customer turn."""

    call_id: str
    turn: TranscriptTurn
    intent: Optional[IntentOut] = None
    retrieval: Optional[RetrievalOut] = None
    nba: Optional[NBAOut] = None
    guardrail: Optional[CheckOut] = None
    blocked: bool = False
    tier_path: list[str] = Field(
        default_factory=list, description="e.g. ['tiny', 'cheap', 'none', 'high', 'safety']"
    )
    cost_usd: float = 0.0
    latency_ms: float = 0.0


class PostCallResult(BaseModel):
    call_id: str
    crm: CRMOut
    guardrail: CheckOut
    ledger: CostLedger

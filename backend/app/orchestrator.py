"""
The orchestrator: the only module that knows the pipeline shape.

Agents are specialists that speak in schema types and know nothing about each
other. This file wires them, applies the code-level gates between them, and
records what each step cost. If you read one file to understand the system, read
this one.

Live turn:
    consent gate -> injection screen (TINY) -> intent (CHEAP)
                 -> retrieval (NONE, local) -> next-best-action (STANDARD/HIGH)
                 -> self-check (code, then SAFETY/HIGH) -> dashboard

Post-call:
    summary + CRM patch + follow-up draft (STANDARD)
                 -> self-check (code + HIGH) -> human approval gate -> CRM write
"""

from __future__ import annotations

import time
from typing import Optional

from app.agents.crm import CRMFollowUpAgent
from app.agents.injection import InjectionScreen
from app.agents.intent import IntentAgent
from app.agents.nba import NextBestActionAgent
from app.agents.selfcheck import SelfCheckAgent
from app.guardrails.pii import redact_text
from app.rag.retriever import build_query, retrieve
from app.schemas import (
    ActionType,
    CheckIn,
    CheckOut,
    CheckResult,
    CheckName,
    CRMIn,
    CRMSnapshot,
    Intent,
    IntentIn,
    ModelTier,
    NBAIn,
    NBAOut,
    PostCallResult,
    RetrievalIn,
    RetrievalOut,
    ScreenIn,
    Severity,
    Speaker,
    TranscriptTurn,
    TurnAssist,
)
from app.telemetry.cost import CostMeter


class ConsentNotGiven(Exception):
    """Raised when a turn is submitted for a call with no consent on record.

    This is the consent guardrail. It is an exception, not a warning: there is
    no code path that processes a customer turn without consent, so an agent
    under time pressure cannot skip the disclosure and have the system carry on
    regardless.
    """


class Orchestrator:
    def __init__(self) -> None:
        self.injection = InjectionScreen()
        self.intent = IntentAgent()
        self.nba = NextBestActionAgent()
        self.selfcheck = SelfCheckAgent()
        self.crm = CRMFollowUpAgent()

    # ------------------------------------------------------------------
    # Live turn
    # ------------------------------------------------------------------

    def handle_turn(
        self,
        *,
        call_id: str,
        turn: TranscriptTurn,
        history: list[TranscriptTurn],
        meter: CostMeter,
        consent_ack: bool,
        crm: Optional[CRMSnapshot] = None,
    ) -> TurnAssist:
        started = time.perf_counter()

        # --- gate 0: consent ------------------------------------------
        if not consent_ack:
            raise ConsentNotGiven(
                f"call {call_id}: no consent on record; refusing to process turns"
            )

        # Agent turns need no assistance — only the customer's words drive the
        # pipeline. Skipping them is also most of the cost saving on a real call.
        if turn.speaker != Speaker.CUSTOMER:
            return TurnAssist(
                call_id=call_id,
                turn=turn,
                tier_path=[],
                latency_ms=(time.perf_counter() - started) * 1000,
            )

        tier_path: list[str] = []
        window = history + [turn]

        # --- 1. injection screen (TINY) -------------------------------
        screen = self.injection.run(
            ScreenIn(utterance=turn.text), meter=meter, turn_index=turn.index
        )
        tier_path.append(ModelTier.TINY.value)

        if screen.is_attack:
            # Stop here. The utterance never reaches a reasoning model, so there
            # is nothing for an injected instruction to act on.
            guard = CheckOut(
                passed=False,
                checks=[
                    CheckResult(
                        name=CheckName.INJECTION_SCREEN,
                        passed=False,
                        detail=(
                            f"Manipulation attempt detected (score {screen.score:.4f}). "
                            "Utterance withheld from all reasoning models; no "
                            "suggestion generated."
                        ),
                        enforced_by="llm",
                        severity=Severity.BLOCK,
                    )
                ],
                blocked_reason="Prompt-injection attempt — pipeline halted at the screen.",
            )
            return TurnAssist(
                call_id=call_id,
                turn=turn,
                guardrail=guard,
                blocked=True,
                tier_path=tier_path,
                cost_usd=meter.total_usd,
                latency_ms=(time.perf_counter() - started) * 1000,
            )

        # --- 2. intent (CHEAP) ----------------------------------------
        intent_out = self.intent.run(
            IntentIn(turns=window, customer_id=crm.customer_id if crm else None),
            meter=meter,
            turn_index=turn.index,
        )
        tier_path.append(ModelTier.CHEAP.value)

        # --- 3. retrieval (NONE — local, zero cost) --------------------
        rag_started = time.perf_counter()
        query = build_query(turn.text, intent_out.intent, intent_out.entities)
        retrieval: RetrievalOut = retrieve(
            RetrievalIn(
                query=query,
                intent=intent_out.intent,
                entities=intent_out.entities,
                k=4,
            )
        )
        meter.record_local(
            "retrieval",
            latency_ms=(time.perf_counter() - rag_started) * 1000,
            turn_index=turn.index,
            note=f"{len(retrieval.citations)} chunks, "
            f"{len(retrieval.dropped_stale)} stale dropped",
        )
        tier_path.append(ModelTier.NONE.value)

        # --- 4. next best action (STANDARD -> HIGH) --------------------
        nba_in = NBAIn(
            intent=intent_out.intent,
            entities=intent_out.entities,
            citations=retrieval.citations,
            crm=crm,
            recent_turns=window[-4:],
            dropoff_risk=intent_out.dropoff_risk,
        )
        # Route once, then hand the decision to the agent. Routing here rather
        # than inside run() keeps the tier visible to the UI and guarantees the
        # tier shown is the tier actually billed.
        chosen_tier, trigger = self.nba.route(nba_in, intent_out.confidence)
        nba_out: NBAOut = self.nba.run(
            nba_in,
            meter=meter,
            turn_index=turn.index,
            tier=chosen_tier,
            trigger=trigger,
        )
        tier_path.append(chosen_tier.value)
        escalated = chosen_tier == ModelTier.HIGH

        # --- 5. self-check (code, then SAFETY/HIGH) -------------------
        guard = self.selfcheck.run(
            CheckIn(
                candidate_say=nba_out.say,
                candidate_why=nba_out.why,
                action_type=nba_out.action_type,
                cited_chunk_ids=nba_out.cited_chunk_ids,
                citations=retrieval.citations,
                consent_ack=consent_ack,
                injection_flagged=False,
                stage="live_turn",
            ),
            meter=meter,
            turn_index=turn.index,
            escalated=escalated,
            dropped_stale=retrieval.dropped_stale,
        )
        tier_path.append(
            (ModelTier.HIGH if escalated else ModelTier.SAFETY).value
        )

        # Apply the guardrail's decisions to the suggestion before it ships.
        if guard.redacted_say:
            nba_out.say = guard.redacted_say
        _, forced = _force_confirmation(nba_out)
        nba_out.requires_human_confirmation = forced

        return TurnAssist(
            call_id=call_id,
            turn=_redact_turn(turn),
            intent=intent_out,
            retrieval=retrieval,
            nba=nba_out,
            guardrail=guard,
            blocked=not guard.passed,
            tier_path=tier_path,
            cost_usd=round(meter.total_usd, 8),
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    # ------------------------------------------------------------------
    # Post-call
    # ------------------------------------------------------------------

    def finalise_call(
        self,
        *,
        call_id: str,
        transcript: list[TranscriptTurn],
        intents_seen: list[Intent],
        max_dropoff_risk: float,
        meter: CostMeter,
        consent_ack: bool,
        crm: Optional[CRMSnapshot] = None,
        do_not_call: bool = False,
    ) -> PostCallResult:
        crm_out = self.crm.run(
            CRMIn(
                call_id=call_id,
                transcript=transcript,
                intents_seen=intents_seen,
                max_dropoff_risk=max_dropoff_risk,
                crm=crm,
            ),
            meter=meter,
            do_not_call=do_not_call,
        )

        # The follow-up draft is the only customer-facing artefact here. When
        # there is none -- the customer converted, or opted out -- the summary
        # is reviewed instead, but as an INTERNAL note: every code check still
        # runs, and the customer-facing conduct policy correctly does not.
        has_draft = crm_out.followup_draft is not None
        candidate = crm_out.followup_draft.body if has_draft else crm_out.summary

        guard = self.selfcheck.run(
            CheckIn(
                candidate_say=candidate,
                candidate_why=f"disposition={crm_out.disposition.value}",
                action_type=ActionType.SCHEDULE_FOLLOWUP,
                cited_chunk_ids=[],
                citations=[],
                consent_ack=consent_ack,
                injection_flagged=False,
                stage="post_call",
                customer_facing=has_draft,
            ),
            meter=meter,
            escalated=True,
        )
        if guard.redacted_say and crm_out.followup_draft:
            crm_out.followup_draft.body = guard.redacted_say

        return PostCallResult(call_id=call_id, crm=crm_out, guardrail=guard, ledger=meter.ledger())


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _force_confirmation(nba: NBAOut) -> tuple[NBAOut, bool]:
    from app.guardrails.rules import check_credit_terms

    _, forced = check_credit_terms(
        nba.say, nba.action_type, nba.requires_human_confirmation
    )
    return nba, forced


def _redact_turn(turn: TranscriptTurn) -> TranscriptTurn:
    """PII never reaches the dashboard, the logs, or the CRM in the clear."""
    return TranscriptTurn(
        index=turn.index,
        speaker=turn.speaker,
        text=redact_text(turn.text),
        ts=turn.ts,
    )


_orchestrator: Optional[Orchestrator] = None


def get_orchestrator() -> Orchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
    return _orchestrator

"""
SelfCheckAgent — the guardrail pass every output must survive.

Runs code checks first, then an LLM adjudication only if the code checks pass.
That ordering is deliberate and is the cost story in miniature: an output that
fails a deterministic check never reaches a paid model at all.

Five checks are Python (`guardrails/rules.py`). One is the 86M injection screen.
One — goal alignment — is genuine model judgement, and it runs on
`gpt-oss-safeguard-20b`, a model purpose-built for policy adjudication rather
than a general model asked to role-play a compliance officer.

Every result carries `enforced_by`, which the dashboard renders distinctly. A
reviewer can see exactly which parts of the safety story hold up against an
adversarial customer and which are model judgement.
"""

from __future__ import annotations

from typing import Optional

from app.agents.base import Agent
from app.config import BUSINESS_GOALS
from app.guardrails import rules
from app.guardrails.pii import redact
from app.llm.router import route_selfcheck
from app.schemas import (
    CheckIn,
    CheckName,
    CheckOut,
    CheckResult,
    ModelTier,
    Severity,
)
from app.telemetry.cost import CostMeter

SYSTEM = f"""\
You are a compliance reviewer for an Indian fintech's inside-sales AI co-pilot.
You are reviewing a suggestion the AI produced for a HUMAN sales agent to say to
a customer about PayFlex Pay-in-3.

Judge it against these business goals:
{BUSINESS_GOALS}

Fail the suggestion if ANY of these are true:
- It predicts, promises, or hints at a credit limit, approval, or eligibility
  outcome. Only the underwriting system decides, after KYC.
- It claims the product is entirely free without noting that a late fee and a
  bounce fee exist.
- It states a figure, fee, or timeframe that is not supported by the knowledge
  base excerpts provided.
- It downplays or omits a downside the customer directly asked about
  (especially credit-score impact).
- It keeps selling after the customer said they are not interested.
- It asks the customer for an OTP, full Aadhaar number, card number, CVV, or a
  password.
- Its tone is pressuring, guilt-tripping, or manufactures false urgency.

Otherwise pass it. Being commercially useful is not a failure — only the rules
above are.

Return ONLY a JSON object:
  passed        boolean
  reason        one sentence explaining the verdict
  severity      one of: info, warn, block

Output JSON only. No prose, no code fences.
"""


class SelfCheckAgent(Agent[CheckIn, CheckOut]):
    name = "self_check"
    tier = ModelTier.SAFETY

    def run(
        self,
        inp: CheckIn,
        *,
        meter: Optional[CostMeter] = None,
        turn_index: Optional[int] = None,
        escalated: bool = False,
        dropped_stale: Optional[list[str]] = None,
    ) -> CheckOut:
        checks: list[CheckResult] = []

        # --- 1. code checks (tier NONE, zero cost) ------------------------
        checks.append(rules.check_consent(inp.consent_ack))
        checks.append(rules.check_injection(inp.injection_flagged))

        redaction = redact(inp.candidate_say)
        checks.append(rules.check_pii(inp.candidate_say, redaction.text))

        # Grounding runs on the REDACTED text, not the raw suggestion. If the
        # model echoes a customer's phone number back, redaction masks it to a
        # placeholder containing no digits — so it is correctly reported as a
        # PII event, and does not also surface as a phantom "ungrounded figure"
        # blaming the model for a number the customer themselves supplied.
        checks.append(
            rules.check_grounding(redaction.text, inp.cited_chunk_ids, inp.citations)
        )
        checks.append(
            rules.check_stale_terms(
                inp.citations, inp.cited_chunk_ids, dropped_stale=dropped_stale
            )
        )
        credit_result, _forced = rules.check_credit_terms(
            redaction.text, inp.action_type, requires_human_confirmation=True
        )
        checks.append(credit_result)

        if meter is not None:
            meter.record_local(
                f"{self.name}:code",
                turn_index=turn_index,
                note="6 deterministic checks",
            )

        code_blocked = [
            c for c in checks if not c.passed and c.severity == Severity.BLOCK
        ]
        if code_blocked:
            # Short-circuit: a failed code check never reaches a paid model.
            return CheckOut(
                passed=False,
                checks=checks,
                redacted_say=redaction.text,
                blocked_reason="; ".join(f"{c.name.value}: {c.detail}" for c in code_blocked),
            )

        # --- 2. LLM adjudication ------------------------------------------
        # Only customer-facing artefacts are adjudicated against the conduct
        # policy. An internal CRM summary is prose describing what happened on
        # the call; judging it by the rules for what an agent may SAY produces
        # confident nonsense ("this asks the customer for an OTP" about a note
        # recording that the customer completed the OTP step). Code checks --
        # PII especially -- still apply to everything.
        if not inp.customer_facing:
            checks.append(
                CheckResult(
                    name=CheckName.GOAL_ALIGNMENT,
                    passed=True,
                    detail=(
                        "Internal artefact — not customer-facing, so the conduct "
                        "policy does not apply. All code checks passed."
                    ),
                    enforced_by="code",
                    severity=Severity.INFO,
                )
            )
            return CheckOut(passed=True, checks=checks, redacted_say=redaction.text)

        tier = route_selfcheck(escalated, inp.stage)
        excerpts = "\n\n---\n\n".join(
            f"[{c.chunk_id}] {c.text}" for c in inp.citations
        ) or "(no excerpts retrieved)"

        user = f"""\
SUGGESTION THE AI WANTS THE AGENT TO SAY:
{redaction.text}

AI'S STATED RATIONALE: {inp.candidate_why or '(none)'}
ACTION TYPE: {inp.action_type.value}
STAGE: {inp.stage}

KNOWLEDGE BASE EXCERPTS THE SUGGESTION MAY DRAW ON:
{excerpts}

Review it. Respond with JSON only."""

        resp = self.llm.complete(
            tier,
            SYSTEM,
            user,
            json_mode=True,
            max_tokens=900,
            temperature=0.0,
            reasoning_effort="low",
            mock_payload={
                "passed": True,
                "reason": "Mock adjudication: consistent with business goals.",
                "severity": "info",
            },
        )
        self._record(meter, resp, turn_index=turn_index)

        data = resp.json(default={"passed": True, "reason": "", "severity": "info"})
        passed = bool(data.get("passed", True))
        try:
            severity = Severity(str(data.get("severity", "warn")).lower())
        except ValueError:
            severity = Severity.WARN
        if not passed and severity == Severity.INFO:
            severity = Severity.BLOCK

        checks.append(
            CheckResult(
                name=CheckName.GOAL_ALIGNMENT,
                passed=passed,
                detail=str(data.get("reason", ""))[:400] or "No reason given.",
                enforced_by="llm",
                severity=severity,
            )
        )

        blocking = [c for c in checks if not c.passed and c.severity == Severity.BLOCK]
        return CheckOut(
            passed=not blocking,
            checks=checks,
            redacted_say=redaction.text,
            blocked_reason=(
                "; ".join(f"{c.name.value}: {c.detail}" for c in blocking)
                if blocking
                else None
            ),
        )

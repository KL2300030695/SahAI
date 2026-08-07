"""
IntentAgent — tier CHEAP (`llama-3.1-8b-instant`).

Runs on every customer turn: classifies intent, pulls entities, and scores
drop-off risk. This is the highest-frequency LLM decision in the system, which
is exactly why it belongs on the cheapest fast model rather than the reasoning
model — at 560 tok/s it returns inside the pause between sentences, and it costs
$0.05/Mtok in.

Its `escalate` output is advisory only. The router ORs it with its own code
rules, so an under-confident or manipulated classifier cannot route a sensitive
turn down to a cheap model.
"""

from __future__ import annotations

import json
from typing import Optional

from app.agents.base import Agent
from app.schemas import Intent, IntentIn, IntentOut, ModelTier, TranscriptTurn
from app.telemetry.cost import CostMeter

SYSTEM = """\
You classify one customer turn in an Indian fintech inside-sales call about
PayFlex Pay-in-3, a zero-cost 3-instalment payment product.

Return ONLY a JSON object with exactly these keys:
  intent          one of: pricing, eligibility, kyc_steps, objection_cost,
                  objection_trust, dropoff_risk, ready_to_convert, smalltalk, other
  confidence      0.0-1.0, your confidence in the intent label
  entities        object of extracted slots; use only keys you actually found from:
                  cart_value, tenure, city, product, merchant, income, existing_loan
  dropoff_risk    0.0-1.0, how likely this customer is to abandon
  escalate        boolean, true if this turn involves credit terms, eligibility,
                  or a complaint that needs careful handling
  rationale       one short sentence

Intent guidance:
- pricing            asking what it costs, instalment amounts, totals
- eligibility        asking if they qualify, what limit they would get
- kyc_steps          asking about signup, documents, Aadhaar, PAN, mandate
- objection_cost     scepticism about hidden charges, "what's the catch"
- objection_trust    credit score worry, privacy worry, "is this a scam"
- dropoff_risk       hesitation, "let me think", "send me details", stalling
- ready_to_convert   agreeing to proceed, asking how to start now
- smalltalk          greetings, consent responses, thanks

Drop-off signals that should push dropoff_risk above 0.6:
"let me think about it", "send me the details", "I'll do it later",
going quiet after Aadhaar or auto-debit is mentioned, repeating a fee question
already answered, or asking to avoid the mandate.

Output JSON only. No prose, no code fences.
"""


def _format_window(turns: list[TranscriptTurn], limit: int = 6) -> str:
    recent = turns[-limit:]
    return "\n".join(f"{t.speaker.value.upper()}: {t.text}" for t in recent)


class IntentAgent(Agent[IntentIn, IntentOut]):
    name = "intent"
    tier = ModelTier.CHEAP

    def run(
        self,
        inp: IntentIn,
        *,
        meter: Optional[CostMeter] = None,
        turn_index: Optional[int] = None,
    ) -> IntentOut:
        if not inp.turns:
            return IntentOut(intent=Intent.OTHER, confidence=0.0)

        latest = inp.turns[-1]
        user = (
            f"Conversation so far:\n{_format_window(inp.turns)}\n\n"
            f"Classify the final CUSTOMER turn: \"{latest.text}\"\n"
            "Respond with JSON only."
        )

        resp = self.llm.complete(
            self.tier,
            SYSTEM,
            user,
            json_mode=True,
            max_tokens=300,
            temperature=0.0,
            mock_payload=_mock_intent(latest.text),
        )
        self._record(meter, resp, turn_index=turn_index)

        data = resp.json(default={})
        return _coerce(data)


def _coerce(data: dict) -> IntentOut:
    """Map a loose model payload onto the strict contract.

    Written defensively on purpose: an 8B model at $0.05/Mtok occasionally
    returns a near-miss label or a stringified float, and one malformed field
    should degrade to a low-confidence classification rather than raise inside a
    live call.
    """
    raw_intent = str(data.get("intent", "other")).strip().lower().replace("-", "_")
    try:
        intent = Intent(raw_intent)
    except ValueError:
        intent = Intent.OTHER

    def _f(key: str, default: float = 0.0) -> float:
        try:
            return max(0.0, min(1.0, float(data.get(key, default))))
        except (TypeError, ValueError):
            return default

    entities_raw = data.get("entities") or {}
    entities: dict[str, str] = {}
    if isinstance(entities_raw, dict):
        entities = {
            str(k): str(v)
            for k, v in entities_raw.items()
            if v not in (None, "", [], {})
        }

    return IntentOut(
        intent=intent,
        confidence=_f("confidence"),
        entities=entities,
        dropoff_risk=_f("dropoff_risk"),
        escalate=bool(data.get("escalate", False)),
        rationale=str(data.get("rationale", ""))[:280],
    )


def _mock_intent(text: str) -> dict:
    """Keyword routing for mock mode. Deterministic, so the scripted demo is
    identical every run."""
    t = text.lower()

    def out(intent: str, conf: float, risk: float, esc: bool, why: str) -> dict:
        return {
            "intent": intent,
            "confidence": conf,
            "entities": {},
            "dropoff_risk": risk,
            "escalate": esc,
            "rationale": why,
        }

    if any(k in t for k in ("think about it", "send me the details", "do it later")):
        return out("dropoff_risk", 0.88, 0.82, True, "Explicit stalling language.")
    if any(k in t for k in ("credit score", "cibil", "enquiry", "scam", "aadhaar", "privacy", "data leak")):
        return out("objection_trust", 0.9, 0.55, True, "Trust or privacy concern.")
    if any(k in t for k in ("catch", "hidden", "processing fee", "free", "interest", "believe")):
        return out("objection_cost", 0.9, 0.45, True, "Scepticism about cost.")
    if any(k in t for k in ("limit", "eligible", "qualify", "approve", "ballpark")):
        return out("eligibility", 0.92, 0.5, True, "Asking about limit or eligibility.")
    if any(k in t for k in ("kyc", "salary slip", "documents", "sign up", "otp", "mandate")):
        return out("kyc_steps", 0.88, 0.3, False, "Onboarding mechanics.")
    if any(k in t for k in ("let's do it", "set it up", "start it", "i'll do the kyc", "do it now")):
        return out("ready_to_convert", 0.93, 0.05, False, "Agreeing to proceed.")
    if any(k in t for k in ("not interested", "don't call", "do not call")):
        return out("dropoff_risk", 0.95, 0.98, True, "Explicit opt-out.")
    if any(k in t for k in ("miss a payment", "late", "instalment", "emi", "total")):
        return out("pricing", 0.85, 0.3, True, "Asking about charges or schedule.")
    if len(t) < 25:
        return out("smalltalk", 0.7, 0.1, False, "Short acknowledgement.")
    return out("other", 0.4, 0.3, False, "No clear category.")

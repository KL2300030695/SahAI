"""
NextBestActionAgent — tier STANDARD, escalating to HIGH.

Produces the suggestion the human sales agent actually reads mid-call: what to
say, why, and what kind of action it is. Routed by `llm/router.py` — routine
turns go to `gpt-oss-20b`, anything touching credit terms, eligibility, trust,
or a high drop-off moment goes to `gpt-oss-120b`.

Three constraints are worth calling out, because they are what make the output
safe to put in front of a customer:

* The prompt is given ONLY the retrieved chunks and told to quote nothing else.
  Whether it complied is not taken on trust — `guardrails/rules.check_grounding`
  verifies every figure against the cited chunk text afterwards.
* `requires_human_confirmation` defaults to True and can only be raised by the
  guardrail, never lowered. The model cannot decide that a credit term is safe
  to state unsupervised.
* The suggestion is written for the human to speak. The AI never talks to the
  customer directly, which is the whole point of a co-pilot rather than a bot.
"""

from __future__ import annotations

import re
from typing import Optional

from app.agents.base import Agent
from app.config import BUSINESS_GOALS
from app.llm.router import RouteContext, route_nba
from app.schemas import (
    ActionType,
    Citation,
    Intent,
    ModelTier,
    NBAIn,
    NBAOut,
    Sentiment,
    Speaker,
)
from app.telemetry.cost import CostMeter

SYSTEM = f"""\
You are the co-pilot for a human inside-sales agent at PayFlex, an Indian
fintech. The product is Pay-in-3: a purchase split into three equal monthly
instalments at zero additional cost.

You are NOT talking to the customer. You write a suggestion the HUMAN AGENT will
read and then say in their own words. Write `say` as natural spoken English, in
the agent's voice, ready to speak aloud.

BUSINESS GOALS you are serving:
{BUSINESS_GOALS}

HARD RULES:
1. Every number, fee, percentage, timeframe or product term you state MUST come
   verbatim from the KNOWLEDGE BASE EXCERPTS provided. If a figure is not in
   those excerpts, do not state it. Say the agent will check instead.
2. Never predict, estimate, promise or hint at a credit limit, an approval, or
   an eligibility outcome. Only the underwriting system decides, after KYC.
3. Never claim the product is "completely free" without qualification. It is
   zero-cost when paid on schedule; a late fee and a bounce fee exist.
4. If the customer says they are not interested, stop selling. Acknowledge,
   confirm the opt-out, close warmly.
5. Be honest about downsides. A customer surprised later is a complaint.
6. Keep `say` under 70 words, and finish every sentence you start. It is read
   aloud into the agent's ear while the customer waits, so a paragraph is
   useless to them — two or three sentences, then stop. If there are more than
   three steps to cover, give the first ones and offer to go through the rest.
7. Never claim an action has already happened. You cannot send emails, send
   SMS, look anything up, or change an account. Do NOT write "I've sent you
   an email", "I've updated your account", or "check your inbox". Phrase any
   action as something the AGENT will do next: "I'll send that across once
   we're done." Telling the customer something was done when it was not is
   the single worst failure available to you.
8. A CALLER WITH A PROBLEM IS NOT A SALES OPPORTUNITY. If the intent is
   `complaint` or `payment_issue`, or the customer sounds frustrated or angry:
   stop selling entirely. Do not mention the product, offers, limits, or
   onboarding. Acknowledge the problem, then route them to the right team
   (`action_type: escalate_human`). Never promise a refund, reversal, waiver,
   or resolution date. If the purchase was never on a PayFlex plan, say so
   plainly rather than implying visibility you do not have.
9. Match the customer's state. If they are busy, be brief and offer a callback.
   If they are confused, slow down and re-explain one thing, not five. If they
   are angry, do not be cheerful at them.

Return ONLY a JSON object with exactly these keys:
  say                          what the agent should say next (string)
  why                          one sentence of rationale for the agent (string)
  action_type                  one of: explain, reassure, quote_terms, send_link,
                               escalate_human, schedule_followup
  cited_chunk_ids              array of chunk_id strings you actually used
  requires_human_confirmation  boolean; true for anything touching credit terms

Output JSON only. No prose, no code fences.
"""


def _format_citations(citations: list[Citation], no_match: bool = False) -> str:
    if no_match:
        # Said explicitly, because "empty" and "searched and found nothing" are
        # different situations and only one of them warrants promising to check.
        return (
            "(THE KNOWLEDGE BASE WAS SEARCHED AND NOTHING MATCHED THIS QUESTION "
            "WELL ENOUGH TO QUOTE.\n"
            "You therefore have NO source for any product fact right now. Do not "
            "answer the question from memory, do not approximate, and do not "
            "reason it out from what you know about similar products. Tell the "
            "customer plainly that you want to give them the exact answer and "
            "will confirm it, then continue the conversation.)"
        )
    if not citations:
        return "(none retrieved — do not state any specific figure)"
    blocks = []
    for c in citations:
        blocks.append(
            f"[chunk_id: {c.chunk_id}] (from: {c.title}, version {c.version})\n{c.text}"
        )
    return "\n\n---\n\n".join(blocks)


def _selling_gate(inp: NBAIn) -> str:
    """A blunt in-prompt instruction when this turn must not be a pitch.

    Stated as its own line rather than buried in the system rules, because it
    inverts the agent's default job and needs to be impossible to miss."""
    if inp.intent in (Intent.COMPLAINT, Intent.PAYMENT_ISSUE):
        return (
            "\n*** DO NOT SELL ON THIS TURN. The customer has a problem to "
            "resolve. Acknowledge it and route them to the right team. Do not "
            "mention the product, offers, limits, or onboarding. ***"
        )
    if inp.sentiment in (Sentiment.ANGRY, Sentiment.FRUSTRATED):
        return (
            "\n*** The customer is upset. Do not pitch. Acknowledge first, and "
            "offer a supervisor if they have raised this before. ***"
        )
    if inp.sentiment is Sentiment.BUSY:
        return "\n*** The customer is short of time. One sentence, then offer a callback. ***"
    return ""


def _format_crm(inp: NBAIn) -> str:
    if not inp.crm:
        return "(no CRM record)"
    c = inp.crm
    lines = [
        f"name: {c.name}",
        f"city: {c.city}",
        f"kyc_status: {c.kyc_status}",
        f"last_disposition: {c.last_disposition or '-'}",
    ]
    if c.past_interactions:
        lines.append("history: " + " | ".join(c.past_interactions[:3]))
    return "\n".join(lines)


class NextBestActionAgent(Agent[NBAIn, NBAOut]):
    name = "next_best_action"
    tier = ModelTier.STANDARD

    def run(
        self,
        inp: NBAIn,
        *,
        meter: Optional[CostMeter] = None,
        turn_index: Optional[int] = None,
        tier: Optional[ModelTier] = None,
        trigger: Optional[str] = None,
    ) -> NBAOut:
        recent = "\n".join(
            f"{t.speaker.value.upper()}: {t.text}" for t in inp.recent_turns[-4:]
        )
        if tier is None:
            tier, trigger = self.route(inp, confidence=1.0)

        user = f"""\
DETECTED INTENT: {inp.intent.value}
CUSTOMER SOUNDS: {inp.sentiment.value}
DROP-OFF RISK: {inp.dropoff_risk:.2f}
EXTRACTED DETAILS: {inp.entities or '(none)'}
{_selling_gate(inp)}

CRM RECORD:
{_format_crm(inp)}

RECENT CONVERSATION:
{recent}

KNOWLEDGE BASE EXCERPTS — the ONLY source you may quote figures from:
{_format_citations(inp.citations, inp.no_confident_match)}

Write the agent's next line. Respond with JSON only."""

        resp = self.llm.complete(
            tier,
            SYSTEM,
            user,
            json_mode=True,
            # Headroom for reasoning tokens, which are billed as completion
            # tokens and count toward this ceiling on the gpt-oss models.
            max_tokens=1400,
            temperature=0.3,
            reasoning_effort="low",
            mock_payload=_mock_nba(inp),
        )
        self._record(meter, resp, turn_index=turn_index, escalation_trigger=trigger)

        return _coerce(resp.json(default={}), inp)

    def route(self, inp: NBAIn, confidence: float) -> tuple[ModelTier, Optional[str]]:
        """The single routing decision for this turn.

        Scoped to what the *customer* said, deliberately. An earlier version
        also fed the retrieved KB text into the credit-terms rule, which sounds
        reasonable and is badly wrong: this is a credit product, so every
        retrieved chunk mentions fees, eligibility or approval, and the rule
        fired on essentially every turn. That escalated ~90% of turns to the
        120B model and quietly erased the cost tiering it was meant to justify.

        The signal that matters is whether the customer raised a sensitive
        topic, not whether the knowledge base happens to contain one.
        """
        customer_text = " ".join(
            t.text for t in inp.recent_turns[-3:] if t.speaker == Speaker.CUSTOMER
        )
        return route_nba(
            RouteContext(
                intent=inp.intent,
                confidence=confidence,
                dropoff_risk=inp.dropoff_risk,
                text=customer_text,
                sentiment=inp.sentiment,
            )
        )


#: Rule 6 says "under 70 words". Allow a little slack before intervening, so a
#: line that merely runs long is left exactly as the model wrote it.
_SAY_WORD_BUDGET = 80

_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")


def _trim_to_sentence(say: str) -> str:
    """Drop a trailing fragment from an over-long or cut-off suggestion.

    Two things produce one: the model ignoring the word budget, and the response
    hitting `max_tokens` mid-string on a retry that dropped strict JSON mode —
    which is how a line ended at "...via a second OTP; we never upload or share
    your Aadhaar number. After" in front of a real agent.

    Only ever cuts at a sentence boundary, and only ever removes text. Truncating
    mid-clause could invert a meaning ("we never share your Aadhaar" is not the
    same claim as its first four words), so a fragment with no earlier boundary
    to fall back on is left alone for the guardrail and the agent to judge rather
    than silently reshaped here.
    """
    if not say:
        return say

    ends = [m.end() for m in _SENTENCE_END.finditer(say)]
    over_budget = len(say.split()) > _SAY_WORD_BUDGET
    unfinished = not ends or ends[-1] != len(say.rstrip())

    if not (over_budget or unfinished):
        return say
    if not ends:
        return say

    if unfinished:
        # Everything up to the last completed sentence; the tail is a fragment.
        say = say[: ends[-1]].rstrip()

    while len(say.split()) > _SAY_WORD_BUDGET and len(ends) > 1:
        ends.pop()
        say = say[: ends[-1]].rstrip()

    return say


def _coerce(data: dict, inp: NBAIn) -> NBAOut:
    raw_action = str(data.get("action_type", "explain")).strip().lower()
    try:
        action = ActionType(raw_action)
    except ValueError:
        action = ActionType.EXPLAIN

    cited = data.get("cited_chunk_ids") or []
    if not isinstance(cited, list):
        cited = []
    valid_ids = {c.chunk_id for c in inp.citations}
    # Drop hallucinated chunk ids rather than letting them satisfy the grounding
    # check by accident.
    cited = [str(c) for c in cited if str(c) in valid_ids]

    say = _trim_to_sentence(str(data.get("say", "")).strip())
    if not say:
        say = (
            "Let me pull up the exact details on that for you — I'd rather quote "
            "you the right number than go from memory."
        )
        action = ActionType.EXPLAIN

    return NBAOut(
        say=say,
        why=str(data.get("why", ""))[:400],
        action_type=action,
        cited_chunk_ids=cited,
        # Default True; the guardrail may force it up, never down.
        requires_human_confirmation=bool(
            data.get("requires_human_confirmation", True)
        ),
    )


def _mock_nba(inp: NBAIn) -> dict:
    """Deterministic canned suggestions for mock mode, cited against whatever the
    retriever actually returned so the grounding check still runs for real."""
    ids = [c.chunk_id for c in inp.citations[:2]]
    intent = inp.intent.value

    canned = {
        "objection_cost": (
            "Fair question, and the honest answer is the merchant pays us a fee "
            "for the sale — that's the whole model. So you repay exactly your "
            "cart value, split three ways. The only charges that exist at all "
            "are 250 rupees if an instalment is missed and 150 if a debit "
            "bounces, and both are avoidable.",
            "explain",
        ),
        "objection_trust": (
            "Two parts to that, and I'll give you both. Paying on time is "
            "reported positively and can help your history. If an instalment "
            "goes 30 days unpaid, that's reported too and it can hurt your "
            "score. So it helps if you pay on time and hurts if you don't.",
            "reassure",
        ),
        "eligibility": (
            "I genuinely can't see or promise a number from here — that's "
            "decided by the system after your KYC, and you'll see it in the app "
            "straight away. What I can tell you is the check itself takes under "
            "a minute.",
            "escalate_human",
        ),
        "kyc_steps": (
            "Less than you'd expect — no salary slips, no bank statements, no "
            "address proof. It's your PAN number and an Aadhaar OTP check, then "
            "the bank mandate. Most people are through in under four minutes.",
            "explain",
        ),
        "dropoff_risk": (
            "Of course, no rush at all. One thing worth doing while we're on the "
            "line is just the first step — the mobile OTP, about thirty seconds. "
            "It's saved for 7 days after that, so you can finish whenever suits.",
            "schedule_followup",
        ),
        "ready_to_convert": (
            "Great, I'll stay on the line. Heads up that there's a second OTP at "
            "the Aadhaar step — that's the one place people pause, so I'll walk "
            "you through it.",
            "explain",
        ),
        "complaint": (
            "I'm sorry, that shouldn't have happened. Let me get you to the team "
            "who can actually fix it rather than have you explain it twice — can "
            "you give me one moment?",
            "escalate_human",
        ),
        "payment_issue": (
            "That's not right and I want it looked at properly. I'm putting you "
            "through to the payments team now — they can see the transaction and "
            "I can't, so I'd rather not guess at it.",
            "escalate_human",
        ),
    }
    say, action = canned.get(
        intent,
        (
            "Let me pull up the exact details on that so I quote you the right "
            "number rather than going from memory.",
            "explain",
        ),
    )
    return {
        "say": say,
        "why": f"Mock response for intent={intent}.",
        "action_type": action,
        "cited_chunk_ids": ids,
        "requires_human_confirmation": intent in ("eligibility", "pricing", "objection_cost"),
    }

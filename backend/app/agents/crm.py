"""
CRMFollowUpAgent — tier STANDARD, post-call.

Writes the call summary, proposes a CRM field diff, classifies the disposition,
and drafts a follow-up for drop-offs.

The word "proposes" is load-bearing. This agent returns a patch and a draft; it
cannot write either. `send_status` is typed as an enum whose only reachable value
here is PENDING_AGENT_APPROVAL, and the function that applies a patch to the
customer record (`crm/db.apply_approved_patch`) requires a named human approver
and is only ever called from the approval endpoint. Human oversight is a state
machine, not a sentence in a prompt.

Two suppression rules run in code, before drafting:
  * an explicit opt-out produces no follow-up at all (TRAI compliance)
  * a `do_not_call` customer produces no follow-up at all
Neither is left to the model's discretion.
"""

from __future__ import annotations

import re
from typing import Optional

from app.agents.base import Agent
from app.guardrails.pii import redact_obj, redact_text
from app.schemas import (
    CRMIn,
    CRMOut,
    Disposition,
    FollowUpDraft,
    Intent,
    ModelTier,
    SendStatus,
)
from app.telemetry.cost import CostMeter

SYSTEM = """\
You summarise a completed inside-sales call for an Indian fintech (PayFlex
Pay-in-3 — a purchase split into three zero-cost instalments) and prepare the
CRM update.

Return ONLY a JSON object with exactly these keys:
  summary          3-4 sentences: what the customer wanted, what was covered,
                   how it ended, and what the next step is. Write it for a
                   colleague picking this account up cold.
  disposition      one of: converted, dropped, callback, not_interested
  dropoff_reason   the SPECIFIC stated reason they did not proceed, or null if
                   they converted. "Not interested" is an outcome, not a reason
                   — give the actual concern (e.g. "uncomfortable sharing
                   Aadhaar", "worried about credit score before a home loan
                   application", "merchant not covered").
  crm_patch        object of fields to update. Allowed keys ONLY:
                   kyc_status, kyc_last_step, city, last_disposition, do_not_call
  followup_channel one of: email, sms, none
  followup_subject short subject line, or "" for sms/none
  followup_body    the follow-up message, or "" if none

Follow-up rules:
- Address the SPECIFIC reason they stopped. A generic "complete your
  application" message is worthless.
- Never include a credit limit, PAN, Aadhaar, or any figure not discussed.
- Keep SMS under 300 characters.
- Do not promise approval, a limit, or eligibility.
- Do not manufacture urgency or deadlines. Nothing expires.

Output JSON only. No prose, no code fences.
"""

_OPT_OUT = re.compile(
    r"\b(not interested|don'?t call|do not call|stop calling|remove me|unsubscribe)\b",
    re.IGNORECASE,
)

_ALLOWED_PATCH_KEYS = {
    "kyc_status",
    "kyc_last_step",
    "city",
    "last_disposition",
    "do_not_call",
}

# Column types the patch must satisfy. The CRM agent is a language model asked
# for JSON; it will occasionally return `kyc_last_step: "aadhaar_verified"` for
# an integer column. An allow-list of keys is not enough on its own -- a
# type-correct value has to reach the database, or the write fails at approval
# time, which is the worst possible moment to discover it.
_PATCH_TYPES: dict[str, type] = {
    "kyc_status": str,
    "kyc_last_step": int,
    "city": str,
    "last_disposition": str,
    "do_not_call": bool,
}

# Named step labels a model tends to produce instead of the step number.
_KYC_STEP_NAMES = {
    "not_started": 0,
    "mobile": 1,
    "mobile_verified": 1,
    "pan": 2,
    "pan_entered": 2,
    "aadhaar": 3,
    "aadhaar_verified": 3,
    "ekyc": 3,
    "mandate": 4,
    "mandate_setup": 4,
    "limit": 5,
    "completed": 5,
    "complete": 5,
}

_DROP = object()  # sentinel: value could not be coerced, drop the key entirely


def _coerce_patch_value(key: str, value: object) -> object:
    """Coerce a model-supplied patch value to the column's type, or drop it.

    Dropping is the right failure mode: a missing field leaves the CRM row
    unchanged, whereas a wrong-typed one breaks the approval write.
    """
    expected = _PATCH_TYPES.get(key)
    if expected is None or isinstance(value, expected) and not (
        expected is int and isinstance(value, bool)
    ):
        return value

    if expected is int:
        if isinstance(value, str):
            key_lower = value.strip().lower()
            if key_lower in _KYC_STEP_NAMES:
                return _KYC_STEP_NAMES[key_lower]
            digits = "".join(ch for ch in value if ch.isdigit())
            if digits:
                return int(digits)
        elif isinstance(value, (int, float)):
            return int(value)
        return _DROP

    if expected is bool:
        if isinstance(value, str):
            return value.strip().lower() in ("true", "yes", "1")
        return bool(value)

    if expected is str:
        return str(value)

    return _DROP


def detect_opt_out(transcript_text: str) -> bool:
    """Code-level opt-out detection. Runs regardless of what the model concludes."""
    return bool(_OPT_OUT.search(transcript_text or ""))


class CRMFollowUpAgent(Agent[CRMIn, CRMOut]):
    name = "crm_followup"
    tier = ModelTier.STANDARD

    def run(
        self,
        inp: CRMIn,
        *,
        meter: Optional[CostMeter] = None,
        turn_index: Optional[int] = None,
        do_not_call: bool = False,
    ) -> CRMOut:
        transcript_text = "\n".join(
            f"{t.speaker.value.upper()}: {t.text}" for t in inp.transcript
        )
        safe_transcript = redact_text(transcript_text)
        opted_out = detect_opt_out(transcript_text)

        intents = ", ".join(sorted({i.value for i in inp.intents_seen})) or "none"
        crm_line = (
            f"{inp.crm.name}, {inp.crm.city}, kyc={inp.crm.kyc_status}"
            if inp.crm
            else "(no CRM record)"
        )

        user = f"""\
CUSTOMER: {crm_line}
INTENTS OBSERVED: {intents}
PEAK DROP-OFF RISK: {inp.max_dropoff_risk:.2f}
CUSTOMER EXPLICITLY OPTED OUT: {"YES" if opted_out else "no"}

FULL TRANSCRIPT (PII already redacted):
{safe_transcript}

Summarise and prepare the CRM update. Respond with JSON only."""

        resp = self.llm.complete(
            self.tier,
            SYSTEM,
            user,
            json_mode=True,
            max_tokens=2000,
            temperature=0.2,
            reasoning_effort="low",
            mock_payload=_mock_crm(opted_out, inp),
        )
        self._record(meter, resp, turn_index=turn_index)

        return _coerce(resp.json(default={}), opted_out=opted_out, do_not_call=do_not_call)


def _coerce(data: dict, *, opted_out: bool, do_not_call: bool) -> CRMOut:
    raw_disp = str(data.get("disposition", "callback")).strip().lower()
    try:
        disposition = Disposition(raw_disp)
    except ValueError:
        disposition = Disposition.CALLBACK

    # Code overrides the model on opt-out. Not negotiable.
    if opted_out:
        disposition = Disposition.NOT_INTERESTED

    patch_raw = data.get("crm_patch") or {}
    patch: dict[str, object] = {}
    if isinstance(patch_raw, dict):
        for k, v in patch_raw.items():
            if k not in _ALLOWED_PATCH_KEYS:
                continue
            coerced = _coerce_patch_value(k, v)
            if coerced is not _DROP:
                patch[k] = coerced
    patch["last_disposition"] = disposition.value
    if opted_out:
        patch["do_not_call"] = True

    # --- follow-up suppression, enforced in code ------------------------
    channel = str(data.get("followup_channel", "none")).strip().lower()
    draft: Optional[FollowUpDraft] = None
    if opted_out or do_not_call:
        draft = None  # never follow up an opt-out or a DNC customer
    elif channel in ("email", "sms"):
        body = str(data.get("followup_body", "")).strip()
        if body:
            draft = FollowUpDraft(
                channel="sms" if channel == "sms" else "email",
                subject=str(data.get("followup_subject", "")).strip()[:140],
                body=redact_text(body)[:1200],
            )

    return CRMOut(
        summary=redact_text(str(data.get("summary", "")).strip())[:1500],
        crm_patch=redact_obj(patch),  # type: ignore[arg-type]
        disposition=disposition,
        dropoff_reason=(
            redact_text(str(data["dropoff_reason"]))[:400]
            if data.get("dropoff_reason")
            else None
        ),
        followup_draft=draft,
        # The only value this agent can ever produce.
        send_status=SendStatus.PENDING_AGENT_APPROVAL,
    )


def _mock_crm(opted_out: bool, inp: CRMIn) -> dict:
    if opted_out:
        return {
            "summary": "Customer asked what the AI assistant does, then tested it "
            "with a manipulation attempt which was declined. Stated they did not "
            "want another credit product and asked not to be contacted again. "
            "Opt-out recorded; no follow-up to be sent.",
            "disposition": "not_interested",
            "dropoff_reason": "Does not want an additional credit product on record.",
            "crm_patch": {"do_not_call": True, "last_disposition": "not_interested"},
            "followup_channel": "none",
            "followup_subject": "",
            "followup_body": "",
        }

    high_risk = inp.max_dropoff_risk >= 0.6
    if high_risk:
        return {
            "summary": "Customer had started KYC and stopped at the Aadhaar step "
            "over privacy concerns, then asked for a limit estimate which the "
            "agent correctly declined to give. Ended undecided and asked for "
            "written details. Progress is saved for 7 days.",
            "disposition": "dropped",
            "dropoff_reason": "Uncomfortable sharing Aadhaar; also frustrated that "
            "the limit is only visible after completing KYC.",
            "crm_patch": {"kyc_status": "in_progress", "kyc_last_step": 3,
                          "last_disposition": "dropped"},
            "followup_channel": "sms",
            "followup_subject": "",
            "followup_body": "Hi, Rahul from PayFlex here. On the Aadhaar step you "
            "paused at — it's an OTP check, nothing is uploaded and we never see "
            "your Aadhaar number. Your progress is saved for 7 days, so you can "
            "pick up right where you left off: {resume_link}",
        }

    return {
        "summary": "Customer was sceptical about hidden charges and referenced an "
        "outdated processing fee. Current fee schedule was confirmed and the "
        "credit-reporting position explained honestly. Customer completed KYC on "
        "the call and their limit is now active.",
        "disposition": "converted",
        "dropoff_reason": None,
        "crm_patch": {"kyc_status": "completed", "kyc_last_step": 5,
                      "last_disposition": "converted"},
        "followup_channel": "none",
        "followup_subject": "",
        "followup_body": "",
    }

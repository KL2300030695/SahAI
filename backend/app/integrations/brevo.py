"""
Brevo transactional email — the step that makes `sent` mean something.

Until now `send_status = "sent"` meant "a human approved a draft that had a
body". Nothing left the building. That is a reasonable state for a prototype and
a dishonest one for a demo, because the word on screen implies delivery.

This is the *transactional* API (`/v3/smtp/email`), not Brevo's marketing
automations product. The distinction matters: an automation is a drip campaign
triggered by a contact entering a list, whereas this is one message, about one
call, released by one named human at one moment. Modelling it as a campaign
would put the send back under a scheduler and quietly undo the approval gate.

Three properties this module is built around.

**It runs after the gate, never around it.** `approve()` refuses blocked drafts,
re-checks rewrites, and resolves the approver from their credential. Only then
is anything handed here. There is no path from an agent to this module.

**A failure is not a `sent`.** If Brevo rejects the message the call stays at
`approved` — a human did say yes — and the error is reported. Writing `sent`
because we tried would make the state machine lie in the one direction that
matters, and the follow-up would be silently lost.

**Redirect is a first-class feature, not a debug flag.** The seeded customers
carry invented addresses. Sending to them means bounces, and enough bounces on a
new Brevo account gets the sender blocked before the demo. `BREVO_REDIRECT_TO`
sends everything to one real inbox with the intended recipient in the subject,
so a demo shows a genuine delivered email without touching a stranger.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

from app.config import get_settings

API_URL = "https://api.brevo.com/v3/smtp/email"
TIMEOUT_S = 10.0

_EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")

_stats = {"sent": 0, "failures": 0, "last_error": "", "last_message_id": ""}


@dataclass(frozen=True)
class SendResult:
    ok: bool
    message_id: str = ""
    detail: str = ""
    recipient: str = ""
    redirected: bool = False


def enabled() -> bool:
    s = get_settings()
    return bool(s.brevo_api_key) and bool(s.brevo_sender_email)


def status() -> dict[str, Any]:
    s = get_settings()
    return {
        "enabled": enabled(),
        "sender": s.brevo_sender_email or None,
        "redirect_to": s.brevo_redirect_to or None,
        "mode": "redirected" if s.brevo_redirect_to else "live recipients",
        **_stats,
    }


def _explain(e: BaseException) -> str:
    """Turn Brevo's error body into the sentence that names the fix.

    The two failures that actually happen during setup are an unverified sender
    and a key with the wrong scope, and both arrive as a bare 401/400 whose JSON
    body carries the useful part. Losing that body costs someone an hour.
    """
    body = ""
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = e.read().decode(errors="replace")[:400]
        except Exception:
            body = ""
        code = e.code
    else:
        return f"{type(e).__name__}: {e}"[:300]

    low = body.lower()
    if "sender" in low and ("not valid" in low or "unrecognised" in low or "unrecognized" in low):
        return (
            "Brevo does not recognise the sender address. Verify "
            f"BREVO_SENDER_EMAIL under Senders & IP in Brevo. — {body}"
        )
    if code == 401:
        return (
            "Brevo rejected the API key. Check it is a Transactional (SMTP) key, "
            f"not a marketing one. — {body}"
        )
    return f"HTTP {code}: {body}"


def _html(body: str) -> str:
    """Plain text to minimal HTML.

    Deliberately not a template with a logo and a footer. The body was written
    by the co-pilot, edited by a human and checked by the guardrail; wrapping it
    in marketing chrome would put unreviewed content in a message whose whole
    claim is that a person approved every word of it.
    """
    escaped = (
        body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    paragraphs = "".join(
        f"<p style='margin:0 0 14px'>{p.strip().replace(chr(10), '<br>')}</p>"
        for p in escaped.split("\n\n")
        if p.strip()
    )
    return (
        "<div style=\"font-family:-apple-system,Segoe UI,sans-serif;"
        "font-size:15px;line-height:1.6;color:#161b22;max-width:560px\">"
        f"{paragraphs}</div>"
    )


def send_email(
    *,
    to_email: str,
    to_name: str,
    subject: str,
    body: str,
    approved_by: str,
    call_id: str,
) -> SendResult:
    """Deliver one approved follow-up. Never called before the approval gate."""
    s = get_settings()
    if not enabled():
        return SendResult(False, detail="Brevo is not configured.")

    intended = (to_email or "").strip()
    redirect = (s.brevo_redirect_to or "").strip()
    recipient = redirect or intended

    if not _EMAIL.match(recipient):
        # Checked before the request so the failure names the cause. A bad
        # address returns a generic 400 from Brevo that reads like a key problem.
        return SendResult(
            False,
            detail=(
                f"No valid address to send to ({recipient!r}). Set the customer's "
                "email, or set BREVO_REDIRECT_TO for a demo."
            ),
        )

    subject = subject.strip() or "About your Pay-in-3 application"
    if redirect and intended and redirect.lower() != intended.lower():
        # The real recipient must survive into the inbox, or a redirected demo
        # shows five identical emails with no way to tell them apart.
        subject = f"[to: {intended}] {subject}"

    payload = {
        "sender": {"email": s.brevo_sender_email, "name": s.brevo_sender_name},
        "to": [{"email": recipient, "name": to_name or recipient}],
        "subject": subject,
        "htmlContent": _html(body),
        "textContent": body,
        # Carried so a message in the Brevo log can be traced back to the call
        # and the person who released it, without opening this codebase.
        "tags": ["sahai", f"call:{call_id}"],
        "headers": {"X-SahAI-Call-Id": call_id, "X-SahAI-Approved-By": approved_by},
    }

    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "api-key": s.brevo_api_key,
            "content-type": "application/json",
            "accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            data = json.loads(r.read() or b"{}")
    except Exception as e:  # noqa: BLE001
        _stats["failures"] += 1
        _stats["last_error"] = _explain(e)
        return SendResult(False, detail=_stats["last_error"], recipient=recipient)

    mid = str(data.get("messageId") or "")
    _stats["sent"] += 1
    _stats["last_message_id"] = mid
    return SendResult(
        True,
        message_id=mid,
        recipient=recipient,
        redirected=bool(redirect and intended and redirect.lower() != intended.lower()),
        detail=f"Delivered to {recipient}.",
    )


def reset_for_tests() -> None:
    _stats.update({"sent": 0, "failures": 0, "last_error": "", "last_message_id": ""})

"""
Twilio Programmable Voice integration.

Two pieces:

* a **TwiML webhook** the carrier fetches when a call arrives, which tells it to
  fork the audio to us; and
* a **Media Streams** WebSocket that receives that audio as base64 mu-law.

`<Start><Stream>` rather than `<Connect><Stream>` is the right verb here, and
the distinction matters: `<Connect>` hands the call *to* the socket and expects
audio back, which is how you build a bot. `<Start>` forks a copy while the call
proceeds normally between the customer and the human agent. This is a co-pilot —
it listens and advises; it never speaks to the customer.

`track="both_tracks"` is the other important flag. The carrier keeps the two
call legs separate, so every frame is labelled `inbound` (the customer) or
`outbound` (the agent). **That gives speaker attribution for free** — the exact
problem the browser-mic path has to solve with a manual toggle, because one
microphone cannot separate voices and Whisper does not diarise.

Plivo and Exotel expose near-identical stream shapes; swapping provider is
mostly a matter of the envelope field names parsed in `parse_message`.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Literal, Optional
from xml.sax.saxutils import escape

from app.schemas import Speaker

# Twilio labels the caller's leg "inbound" and what it plays to the caller
# "outbound". On an inside-sales call the caller is the customer and the agent
# is on the outbound leg.
TRACK_TO_SPEAKER: dict[str, Speaker] = {
    "inbound": Speaker.CUSTOMER,
    "outbound": Speaker.AGENT,
}


def build_twiml(
    stream_url: str,
    *,
    call_id: str,
    greeting: Optional[str] = None,
    track: str = "both_tracks",
) -> str:
    """TwiML instructing the carrier to fork call audio to our socket.

    The greeting is the consent disclosure, and it is spoken by the platform
    before anything is streamed. That is deliberate: consent has to be on record
    before the co-pilot processes a single frame, and putting it in the TwiML
    means it cannot be skipped by an agent under time pressure — the same
    property the dashboard's consent gate has, enforced one layer earlier.
    """
    greeting_verb = (
        f'  <Say voice="Polly.Aditi">{escape(greeting)}</Say>\n' if greeting else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<Response>\n"
        f"{greeting_verb}"
        "  <Start>\n"
        f'    <Stream url="{escape(stream_url)}" track="{track}">\n'
        f'      <Parameter name="call_id" value="{escape(call_id)}" />\n'
        "    </Stream>\n"
        "  </Start>\n"
        "  <Pause length=\"3600\" />\n"
        "</Response>\n"
    )


@dataclass
class MediaEvent:
    """One decoded frame from the carrier's stream."""

    kind: Literal["connected", "start", "media", "stop", "mark", "unknown"]
    payload: bytes = b""
    track: str = "inbound"
    speaker: Speaker = Speaker.CUSTOMER
    stream_sid: str = ""
    call_sid: str = ""
    call_id: str = ""
    sequence: int = 0


def parse_message(msg: dict[str, Any]) -> MediaEvent:
    """Parse one Media Streams frame.

    Provider-specific parsing is confined to this function so a different
    carrier means editing here rather than in the pipeline.
    """
    event = str(msg.get("event", "unknown"))

    if event == "start":
        start = msg.get("start", {}) or {}
        params = start.get("customParameters", {}) or {}
        return MediaEvent(
            kind="start",
            stream_sid=str(msg.get("streamSid", "")),
            call_sid=str(start.get("callSid", "")),
            # Our own id, threaded through the TwiML <Parameter>. Without it we
            # would have no way to tie the audio to the session the agent's
            # dashboard is watching.
            call_id=str(params.get("call_id", "")),
        )

    if event == "media":
        media = msg.get("media", {}) or {}
        track = str(media.get("track", "inbound"))
        raw = media.get("payload", "")
        try:
            payload = base64.b64decode(raw) if raw else b""
        except Exception:
            payload = b""
        return MediaEvent(
            kind="media",
            payload=payload,
            track=track,
            speaker=TRACK_TO_SPEAKER.get(track, Speaker.CUSTOMER),
            stream_sid=str(msg.get("streamSid", "")),
            sequence=int(media.get("chunk", 0) or 0),
        )

    if event in ("connected", "stop", "mark"):
        return MediaEvent(kind=event, stream_sid=str(msg.get("streamSid", "")))

    return MediaEvent(kind="unknown")


def verify_signature(
    auth_token: str, url: str, params: dict[str, str], signature: str
) -> bool:
    """Validate Twilio's `X-Twilio-Signature` on a webhook.

    The webhook has to be publicly reachable for the carrier to call it, which
    means anyone else can call it too. Signature checking is what stops a
    stranger opening call sessions on your account. Skipped when no auth token
    is configured, so the simulator and local development still work.
    """
    if not auth_token:
        return True
    import hashlib
    import hmac

    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(
        auth_token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1
    ).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature or "")

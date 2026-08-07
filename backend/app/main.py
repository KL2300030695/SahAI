"""
FastAPI surface.

REST for setup and post-call review, a WebSocket for the live agent-assist feed.

The two endpoints worth reading closely are:

* `POST /api/calls/{call_id}/consent` — nothing else works until this is called.
  The WebSocket refuses to stream and the orchestrator raises. Consent is a gate
  in the code path, not a checkbox in the UI.
* `POST /api/calls/{call_id}/approve` — the only route that writes an
  AI-proposed patch onto a customer record, and it requires a named human
  approver. No agent can reach it.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    File,
)
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import (
    BACKEND_DIR,
    BUSINESS_GOALS,
    FRONTIER_BASELINE,
    PRICING,
    ensure_runtime_dirs,
    get_settings,
    price_audio_seconds,
)
from app.crm.db import (
    apply_approved_patch,
    get_crm_snapshot,
    get_session,
    init_db,
    is_do_not_call,
    record_cost,
    session_scope,
)
from app.crm.models import Call, Customer
from app.llm.client import get_llm, is_probably_hallucination
from app.llm.router import describe_routing
from app.orchestrator import ConsentNotGiven, get_orchestrator
from app.schemas import Intent, ModelTier, Sentiment, Speaker, TranscriptTurn
from app.telemetry.cost import CostMeter
from app.telephony.audio import UtteranceBuffer
from app.telephony.twilio import (
    TRACK_TO_SPEAKER,
    build_twiml,
    parse_message,
    verify_signature,
)

TRANSCRIPTS = BACKEND_DIR / "app" / "seed" / "transcripts"
UPLOADS = BACKEND_DIR / "uploads"

settings = get_settings()

app = FastAPI(
    title="SahAI — AI Voice Co-Pilot for Inside Sales",
    version="1.0.0",
    description="Multi-agent, cost-tiered, guardrailed call assistance.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    ensure_runtime_dirs()
    init_db()


# ---------------------------------------------------------------------------
# In-memory live-call state. A hackathon MVP runs one process; a real
# deployment would move this to Redis so the WebSocket can be served by any
# worker. Called out rather than hidden.
# ---------------------------------------------------------------------------


class LiveCall:
    def __init__(self, call_id: str, customer_id: str, agent_name: str) -> None:
        self.call_id = call_id
        self.customer_id = customer_id
        self.agent_name = agent_name
        self.consent_ack = False
        self.consent_at: Optional[datetime] = None
        self.history: list[TranscriptTurn] = []
        self.intents_seen: list[Intent] = []
        self.sentiments_seen: list[Sentiment] = []
        self.buying_signals: list[str] = []
        self.max_dropoff_risk = 0.0
        self.meter = CostMeter(call_id)
        self.assists: list[dict[str, Any]] = []
        self.finalised: Optional[dict[str, Any]] = None

        # Phone calls are driven by the carrier's socket, not by the agent's
        # browser, so the dashboard has to observe rather than drive. Each
        # observer gets its own queue; a slow or dead observer is dropped rather
        # than allowed to block the call.
        self.subscribers: list[asyncio.Queue] = []
        self.source: str = "browser"  # browser | phone | scripted
        self.phone_number: str = ""
        self.ended: bool = False

    def observe(self, assist: Any) -> None:
        """Accumulate per-turn signals the post-call summariser needs.

        One method rather than repeating the same four lines at each of the
        scripted, browser-mic, upload and telephony call sites -- which is how
        the sentiment and buying-signal fields would otherwise have been added
        to three of them and quietly forgotten in the fourth.
        """
        if not assist.intent:
            return
        self.intents_seen.append(assist.intent.intent)
        self.sentiments_seen.append(assist.intent.sentiment)
        self.buying_signals.extend(assist.intent.buying_signals)
        self.max_dropoff_risk = max(
            self.max_dropoff_risk, assist.intent.dropoff_risk
        )

    def publish(self, message: dict[str, Any]) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                self.subscribers.remove(q)


LIVE: dict[str, LiveCall] = {}


def _load_transcript(call_id: str) -> dict:
    for p in sorted(TRANSCRIPTS.glob("*.json")):
        data = json.loads(p.read_text(encoding="utf-8"))
        if data["call_id"] == call_id:
            return data
    raise HTTPException(404, f"unknown call_id {call_id!r}")


def _turns(data: dict) -> list[TranscriptTurn]:
    return [
        TranscriptTurn(
            index=t["index"],
            speaker=Speaker(t["speaker"]),
            text=t["text"],
            ts=float(t["ts"]),
        )
        for t in data["turns"]
    ]


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "mode": "mock" if settings.mock_mode else "live",
        "provider": "groq",
    }


@app.get("/api/policy")
def policy() -> dict:
    """The routing policy, model tiers, pricing, and business goals the system
    checks itself against. Exposed so the cost and safety claims are
    inspectable rather than assertions on a slide."""
    tiers = {}
    for tier in (
        ModelTier.TINY,
        ModelTier.CHEAP,
        ModelTier.STANDARD,
        ModelTier.HIGH,
        ModelTier.SAFETY,
        ModelTier.STT,
    ):
        model = settings.model_for(tier)
        rate = PRICING.get(model)
        tiers[tier.value] = {
            "model": model,
            "usd_per_mtok_in": rate[0] if rate else None,
            "usd_per_mtok_out": rate[1] if rate else None,
        }
    return {
        "tiers": tiers,
        "zero_cost_steps": [
            "retrieval (local ONNX MiniLM embeddings + BM25)",
            "PII redaction (regex)",
            "consent gate",
            "grounding check",
            "stale-terms check",
            "credit-term confirmation forcing",
        ],
        "routing": describe_routing(),
        "business_goals": BUSINESS_GOALS,
        "frontier_baseline": FRONTIER_BASELINE,
    }


@app.get("/api/calls")
def list_calls() -> list[dict]:
    out = []
    for p in sorted(TRANSCRIPTS.glob("*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        out.append(
            {
                "call_id": d["call_id"],
                "customer_id": d["customer_id"],
                "agent_name": d["agent_name"],
                "outcome": d["outcome"],
                "scenario": d.get("scenario", ""),
                "turns": len(d["turns"]),
            }
        )
    return out


@app.get("/api/calls/{call_id}")
def get_call(call_id: str, db: Session = Depends(get_session)) -> dict:
    data = _load_transcript(call_id)
    crm = get_crm_snapshot(db, data["customer_id"])
    live = LIVE.get(call_id)
    return {
        "call_id": data["call_id"],
        "customer_id": data["customer_id"],
        "agent_name": data["agent_name"],
        "outcome": data["outcome"],
        "scenario": data.get("scenario", ""),
        "turn_count": len(data["turns"]),
        "crm": crm.model_dump() if crm else None,
        "consent_ack": bool(live and live.consent_ack),
        "mode": "mock" if settings.mock_mode else "live",
    }


# ---------------------------------------------------------------------------
# Consent gate
# ---------------------------------------------------------------------------


class ConsentBody(BaseModel):
    consent_ack: bool
    agent_name: str = ""


@app.post("/api/calls/{call_id}/consent")
def record_consent(call_id: str, body: ConsentBody) -> dict:
    """Open a call session. Nothing downstream runs until this succeeds."""
    data = _load_transcript(call_id)
    if not body.consent_ack:
        raise HTTPException(
            400,
            "Consent declined. Recording and AI assistance must stay disabled; "
            "offer the customer a callback on a non-recorded line.",
        )

    live = LiveCall(
        call_id=call_id,
        customer_id=data["customer_id"],
        agent_name=body.agent_name or data["agent_name"],
    )
    live.consent_ack = True
    live.consent_at = datetime.now(timezone.utc)
    LIVE[call_id] = live

    with session_scope() as s:
        call = s.get(Call, call_id)
        if not call:
            call = Call(call_id=call_id, customer_id=data["customer_id"])
            s.add(call)
        call.agent_name = live.agent_name
        call.consent_ack = True
        call.consent_at = live.consent_at
        call.transcript_json = json.dumps(data["turns"])

    return {"call_id": call_id, "consent_ack": True, "consent_at": live.consent_at}


# ---------------------------------------------------------------------------
# Live agent-assist stream
# ---------------------------------------------------------------------------


@app.websocket("/ws/call/{call_id}")
async def ws_call(ws: WebSocket, call_id: str) -> None:
    """Replay a transcript turn-by-turn, pushing agent assistance per turn.

    Scripted playback rather than live mic: deterministic, no STT dependency in
    the demo path, and the pipeline behind it is identical to what a real audio
    stream would drive. Whisper is available at /api/transcribe for real audio.
    """
    await ws.accept()
    try:
        data = _load_transcript(call_id)
    except HTTPException:
        await ws.send_json({"type": "error", "message": f"unknown call {call_id}"})
        await ws.close()
        return

    live = LIVE.get(call_id)
    if live is None or not live.consent_ack:
        await ws.send_json(
            {
                "type": "blocked",
                "reason": "consent_not_recorded",
                "message": (
                    "No consent on record for this call. The orchestrator will not "
                    "process turns until consent is captured — this is a code-level "
                    "gate, not a UI reminder."
                ),
            }
        )
        await ws.close()
        return

    orch = get_orchestrator()
    with session_scope() as s:
        crm = get_crm_snapshot(s, live.customer_id)

    await ws.send_json(
        {
            "type": "started",
            "call_id": call_id,
            "turn_count": len(data["turns"]),
            "mode": "mock" if settings.mock_mode else "live",
        }
    )

    try:
        for turn in _turns(data):
            await ws.send_json(
                {"type": "turn", "turn": json.loads(turn.model_dump_json())}
            )

            if turn.speaker != Speaker.CUSTOMER:
                await asyncio.sleep(settings.playback_interval_seconds * 0.45)
                continue

            await ws.send_json({"type": "thinking", "turn_index": turn.index})

            try:
                assist = await asyncio.to_thread(
                    orch.handle_turn,
                    call_id=call_id,
                    turn=turn,
                    history=list(live.history),
                    meter=live.meter,
                    consent_ack=live.consent_ack,
                    crm=crm,
                )
            except ConsentNotGiven as e:
                await ws.send_json({"type": "blocked", "reason": str(e)})
                break

            live.history.append(turn)
            if assist.intent:
                live.observe(assist)

            payload = json.loads(assist.model_dump_json())
            live.assists.append(payload)
            await ws.send_json({"type": "assist", "assist": payload})
            await ws.send_json(
                {
                    "type": "ledger",
                    "ledger": json.loads(live.meter.ledger().model_dump_json()),
                    "frontier_usd": live.meter.frontier_baseline_usd(),
                }
            )
            await asyncio.sleep(settings.playback_interval_seconds)

        # Persist the ledger once the call has played out.
        with session_scope() as s:
            for row in live.meter.rows:
                record_cost(s, row)

        await ws.send_json({"type": "call_ended", "call_id": call_id})
    except WebSocketDisconnect:
        return
    finally:
        try:
            await ws.close()
        except Exception:
            pass

    # history kept in LIVE for the post-call step
    live.history = live.history


# ---------------------------------------------------------------------------
# Live voice call
# ---------------------------------------------------------------------------


class LiveStartBody(BaseModel):
    customer_id: str
    agent_name: str = "Agent"
    consent_ack: bool = False


@app.post("/api/live/start")
def live_start(body: LiveStartBody, db: Session = Depends(get_session)) -> dict:
    """Open a live microphone call session.

    Consent is captured here, in the same call that creates the session, so the
    same gate covers scripted and live calls alike — there is no second code
    path where a live call could start unconsented.
    """
    if not body.consent_ack:
        raise HTTPException(
            400,
            "Consent declined. Recording and AI assistance must stay disabled; "
            "offer the customer a callback on a non-recorded line.",
        )
    customer = db.get(Customer, body.customer_id)
    if not customer:
        raise HTTPException(404, f"unknown customer {body.customer_id!r}")

    call_id = f"live-{uuid.uuid4().hex[:8]}"
    live = LiveCall(
        call_id=call_id, customer_id=body.customer_id, agent_name=body.agent_name
    )
    live.consent_ack = True
    live.consent_at = datetime.now(timezone.utc)
    LIVE[call_id] = live

    with session_scope() as s:
        s.add(
            Call(
                call_id=call_id,
                customer_id=body.customer_id,
                agent_name=body.agent_name,
                consent_ack=True,
                consent_at=live.consent_at,
            )
        )

    return {
        "call_id": call_id,
        "customer_id": body.customer_id,
        "consent_ack": True,
        "stt_model": settings.model_stt,
    }


@app.websocket("/ws/live/{call_id}")
async def ws_live(ws: WebSocket, call_id: str) -> None:
    """Live microphone co-pilot.

    Protocol: the client sends one **complete** audio blob per utterance as a
    binary frame, preceded by a small JSON frame naming the speaker. Each blob
    is a self-contained webm file, because MediaRecorder chunks after the first
    carry no header and are not independently decodable — so the browser stops
    and restarts the recorder at each silence boundary rather than slicing a
    continuous stream.

    Segmenting on silence rather than on a fixed timer is what makes this map
    cleanly onto the existing pipeline: one utterance in, one `TurnAssist` out,
    identical to the scripted path.
    """
    await ws.accept()

    live = LIVE.get(call_id)
    if live is None or not live.consent_ack:
        await ws.send_json(
            {
                "type": "blocked",
                "reason": "consent_not_recorded",
                "message": (
                    "No consent on record for this call. The orchestrator will not "
                    "process audio until consent is captured."
                ),
            }
        )
        await ws.close()
        return

    orch = get_orchestrator()
    llm = get_llm()
    with session_scope() as s:
        crm = get_crm_snapshot(s, live.customer_id)

    await ws.send_json(
        {
            "type": "ready",
            "call_id": call_id,
            "stt_model": settings.model_stt,
            "mode": "mock" if settings.mock_mode else "live",
        }
    )

    # The speaker of the *next* binary frame. A single microphone cannot
    # separate the agent from the customer, and Whisper does not diarise, so the
    # UI states who is talking rather than the system guessing. Called out
    # plainly instead of pretending diarization is happening.
    next_speaker = Speaker.CUSTOMER
    turn_index = len(live.history)

    try:
        while True:
            message = await ws.receive()

            if message.get("type") == "websocket.disconnect":
                break

            # --- control frame ---
            if (text := message.get("text")) is not None:
                try:
                    ctrl = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if ctrl.get("action") == "speaker":
                    next_speaker = (
                        Speaker.AGENT
                        if ctrl.get("speaker") == "agent"
                        else Speaker.CUSTOMER
                    )
                elif ctrl.get("action") == "end":
                    break
                continue

            # --- audio frame ---
            audio = message.get("bytes")
            if not audio:
                continue

            await ws.send_json({"type": "transcribing", "bytes": len(audio)})

            try:
                transcript, seconds = await asyncio.to_thread(
                    llm.transcribe_bytes, audio, "utterance.webm"
                )
            except Exception as e:  # noqa: BLE001 — surface STT failures to the UI
                await ws.send_json({"type": "stt_error", "message": str(e)[:300]})
                continue

            live.meter.record_stt(
                "whisper",
                settings.model_stt,
                price_audio_seconds(settings.model_stt, seconds),
                0.0,
            )

            # Whisper answers silence with its training priors, not with
            # nothing. Filler that reaches history is fed to the intent
            # classifier on every later turn, so it must be dropped here.
            if is_probably_hallucination(transcript, seconds):
                await ws.send_json(
                    {
                        "type": "transcript_skipped",
                        "text": transcript,
                        "seconds": seconds,
                        "reason": "no speech detected — filtered as silence filler",
                    }
                )
                continue

            turn = TranscriptTurn(
                index=turn_index,
                speaker=next_speaker,
                text=transcript,
                ts=float(turn_index),
            )
            turn_index += 1

            from app.guardrails.pii import redact

            redacted = redact(transcript)
            await ws.send_json(
                {
                    "type": "transcript",
                    "turn": {
                        "index": turn.index,
                        "speaker": turn.speaker.value,
                        "text": redacted.text,
                        "ts": turn.ts,
                    },
                    "pii_redacted": redacted.found,
                    "seconds": seconds,
                }
            )

            if turn.speaker != Speaker.CUSTOMER:
                live.history.append(turn)
                continue

            await ws.send_json({"type": "thinking", "turn_index": turn.index})

            try:
                assist = await asyncio.to_thread(
                    orch.handle_turn,
                    call_id=call_id,
                    turn=turn,
                    history=list(live.history),
                    meter=live.meter,
                    consent_ack=live.consent_ack,
                    crm=crm,
                )
            except ConsentNotGiven as e:
                await ws.send_json({"type": "blocked", "reason": str(e)})
                break

            live.history.append(turn)
            if assist.intent:
                live.observe(assist)

            payload = json.loads(assist.model_dump_json())
            live.assists.append(payload)
            await ws.send_json({"type": "assist", "assist": payload})
            await ws.send_json(
                {
                    "type": "ledger",
                    "ledger": json.loads(live.meter.ledger().model_dump_json()),
                    "frontier_usd": live.meter.frontier_baseline_usd(),
                }
            )
    except WebSocketDisconnect:
        pass
    finally:
        with session_scope() as s:
            for row in live.meter.rows:
                record_cost(s, row)
        try:
            await ws.send_json({"type": "call_ended", "call_id": call_id})
            await ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Telephony — real phone calls
# ---------------------------------------------------------------------------


@app.get("/api/telephony/config")
def telephony_config() -> dict:
    """What a carrier needs pointing at, and whether we are ready to receive."""
    ready = bool(settings.public_base_url)
    return {
        "ready": ready,
        "voice_webhook": (
            f"{settings.public_base_url.rstrip('/')}/api/telephony/voice"
            if ready
            else None
        ),
        "stream_url": settings.stream_wss_url or None,
        "signature_verification": bool(settings.twilio_auth_token),
        "default_customer_id": settings.default_customer_id,
        "hint": (
            "Set PUBLIC_BASE_URL to an https tunnel (ngrok/cloudflared) and point "
            "your Twilio number's Voice webhook at /api/telephony/voice."
            if not ready
            else "Point your Twilio number's Voice webhook at the URL above."
        ),
    }


@app.get("/api/telephony/active")
def telephony_active() -> list[dict]:
    """Phone calls currently in progress, for the dashboard to attach to."""
    return [
        {
            "call_id": c.call_id,
            "customer_id": c.customer_id,
            "phone_number": c.phone_number,
            "source": c.source,
            "turns": len(c.history),
            "ended": c.ended,
        }
        for c in LIVE.values()
        if c.source == "phone" and not c.ended
    ]


@app.post("/api/telephony/voice")
async def telephony_voice(request: Request) -> Response:
    """TwiML webhook — the carrier fetches this when a call connects.

    Creates the session, records consent, and returns instructions telling the
    carrier to speak the disclosure and then fork call audio to our socket.

    The disclosure lives in the TwiML rather than in an agent's script on
    purpose: it is spoken by the platform before a single audio frame is
    streamed, so it cannot be skipped. Same property as the dashboard's consent
    gate, enforced one layer earlier.
    """
    form = dict((await request.form()))  # type: ignore[arg-type]
    params = {k: str(v) for k, v in form.items()}

    if settings.twilio_auth_token:
        url = str(request.url)
        signature = request.headers.get("X-Twilio-Signature", "")
        if not verify_signature(settings.twilio_auth_token, url, params, signature):
            raise HTTPException(403, "invalid Twilio signature")

    customer_id = (
        request.query_params.get("customer_id") or settings.default_customer_id
    )
    with session_scope() as s:
        if not s.get(Customer, customer_id):
            raise HTTPException(404, f"unknown customer {customer_id!r}")

    call_id = f"tel-{uuid.uuid4().hex[:8]}"
    live = LiveCall(call_id=call_id, customer_id=customer_id, agent_name="Phone agent")
    live.consent_ack = True
    live.consent_at = datetime.now(timezone.utc)
    live.source = "phone"
    live.phone_number = params.get("From", "")
    LIVE[call_id] = live

    with session_scope() as s:
        s.add(
            Call(
                call_id=call_id,
                customer_id=customer_id,
                agent_name="Phone agent",
                consent_ack=True,
                consent_at=live.consent_at,
            )
        )

    stream_url = settings.stream_wss_url
    if not stream_url:
        raise HTTPException(
            500,
            "PUBLIC_BASE_URL is not set, so there is no reachable stream URL for "
            "the carrier to connect back to.",
        )

    twiml = build_twiml(
        stream_url,
        call_id=call_id,
        greeting=(
            "Thanks for calling PayFlex. Before we begin, please note this call "
            "may be recorded and is A I assisted, to help us give you accurate "
            "information. Connecting you now."
        ),
    )
    return Response(content=twiml, media_type="application/xml")


@app.websocket("/ws/telephony/stream")
async def ws_telephony(ws: WebSocket) -> None:
    """Twilio Media Streams socket — carrier audio in, assists out to observers.

    Each party's audio arrives on a separate labelled track, so speaker
    attribution is exact here rather than declared by the user. That is the one
    thing a phone call gives us that a single laptop microphone cannot.
    """
    await ws.accept()

    orch = get_orchestrator()
    llm = get_llm()
    live: Optional[LiveCall] = None
    crm = None
    # One buffer per leg: the customer and the agent talk over each other, and
    # mixing their frames into a single buffer would produce garbage.
    buffers: dict[str, UtteranceBuffer] = {
        "inbound": UtteranceBuffer(),
        "outbound": UtteranceBuffer(),
    }

    # Utterances must be handled in the order they closed. Transcription latency
    # varies by clip, so firing tasks concurrently delivered a scrambled
    # transcript in testing -- "My friend told me..." arrived before "Honestly I
    # don't believe...". Order is not cosmetic here: conversation history is fed
    # to the intent classifier on every turn and to the summariser at the end.
    #
    # A lock rather than unbounded concurrency. Utterances are naturally spaced
    # by the speaker pausing, so the queueing cost is small next to producing a
    # transcript that reads backwards.
    order_lock = asyncio.Lock()

    async def process(wav: bytes, speaker: Speaker) -> None:
        nonlocal live, crm
        if live is None:
            return
        async with order_lock:
            await _process_locked(wav, speaker)

    async def _process_locked(wav: bytes, speaker: Speaker) -> None:
        nonlocal live, crm
        if live is None:
            return
        try:
            text, seconds = await asyncio.to_thread(
                llm.transcribe_bytes, wav, "call.wav"
            )
        except Exception as e:  # noqa: BLE001
            live.publish({"type": "stt_error", "message": str(e)[:300]})
            return

        live.meter.record_stt(
            "whisper",
            settings.model_stt,
            price_audio_seconds(settings.model_stt, seconds),
            0.0,
        )
        if is_probably_hallucination(text, seconds):
            return

        turn = TranscriptTurn(
            index=len(live.history), speaker=speaker, text=text, ts=float(len(live.history))
        )
        from app.guardrails.pii import redact

        red = redact(text)
        live.publish(
            {
                "type": "transcript",
                "turn": {
                    "index": turn.index,
                    "speaker": speaker.value,
                    "text": red.text,
                    "ts": turn.ts,
                },
                "pii_redacted": red.found,
                "seconds": seconds,
            }
        )

        if speaker != Speaker.CUSTOMER:
            live.history.append(turn)
            return

        live.publish({"type": "thinking", "turn_index": turn.index})
        assist = await asyncio.to_thread(
            orch.handle_turn,
            call_id=live.call_id,
            turn=turn,
            history=list(live.history),
            meter=live.meter,
            consent_ack=live.consent_ack,
            crm=crm,
        )
        live.history.append(turn)
        if assist.intent:
            live.observe(assist)

        payload = json.loads(assist.model_dump_json())
        live.assists.append(payload)
        live.publish({"type": "assist", "assist": payload})
        live.publish(
            {
                "type": "ledger",
                "ledger": json.loads(live.meter.ledger().model_dump_json()),
                "frontier_usd": live.meter.frontier_baseline_usd(),
            }
        )

    try:
        while True:
            raw = await ws.receive_text()
            try:
                event = parse_message(json.loads(raw))
            except json.JSONDecodeError:
                continue

            if event.kind == "start":
                live = LIVE.get(event.call_id)
                if live is None:
                    await ws.close()
                    return
                with session_scope() as s:
                    crm = get_crm_snapshot(s, live.customer_id)
                live.publish(
                    {
                        "type": "phone_connected",
                        "call_id": live.call_id,
                        "from": live.phone_number,
                    }
                )

            elif event.kind == "media" and live is not None:
                buf = buffers.setdefault(event.track, UtteranceBuffer())
                wav = buf.add(event.payload)
                if wav:
                    # Fire and forget: transcription takes far longer than the
                    # 20ms frame cadence, so awaiting it here would stall the
                    # socket and drop the caller's audio.
                    asyncio.create_task(process(wav, event.speaker))

            elif event.kind == "stop":
                break

    except WebSocketDisconnect:
        pass
    finally:
        if live is not None:
            for track, buf in buffers.items():
                tail = buf.flush()
                if tail:
                    await process(tail, TRACK_TO_SPEAKER.get(track, Speaker.CUSTOMER))
            live.ended = True
            live.publish({"type": "call_ended", "call_id": live.call_id})
            with session_scope() as s:
                for row in live.meter.rows:
                    record_cost(s, row)
        try:
            await ws.close()
        except Exception:
            pass


@app.websocket("/ws/observe/{call_id}")
async def ws_observe(ws: WebSocket, call_id: str) -> None:
    """Read-only view of a call driven by someone else.

    The agent's dashboard uses this for phone calls: the carrier's socket drives
    the pipeline, and the browser watches. Sends the backlog first so a dashboard
    attaching mid-call is not missing the turns that already happened.
    """
    await ws.accept()
    live = LIVE.get(call_id)
    if live is None:
        await ws.send_json({"type": "error", "message": f"unknown call {call_id}"})
        await ws.close()
        return

    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    live.subscribers.append(queue)
    try:
        await ws.send_json(
            {
                "type": "attached",
                "call_id": call_id,
                "source": live.source,
                "from": live.phone_number,
                "backlog": len(live.assists),
            }
        )
        for a in live.assists:
            await ws.send_json({"type": "assist", "assist": a})
        if live.meter.rows:
            await ws.send_json(
                {
                    "type": "ledger",
                    "ledger": json.loads(live.meter.ledger().model_dump_json()),
                    "frontier_usd": live.meter.frontier_baseline_usd(),
                }
            )

        while True:
            msg = await queue.get()
            await ws.send_json(msg)
            if msg.get("type") == "call_ended":
                break
    except WebSocketDisconnect:
        pass
    finally:
        if queue in live.subscribers:
            live.subscribers.remove(queue)
        try:
            await ws.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Post-call
# ---------------------------------------------------------------------------


@app.post("/api/calls/{call_id}/finalise")
def finalise(call_id: str) -> dict:
    live = LIVE.get(call_id)
    if live is None:
        raise HTTPException(400, "call not started; record consent first")
    if not live.history:
        raise HTTPException(400, "no turns processed yet")

    # A live voice call has no seed transcript file -- its transcript is whatever
    # the microphone produced. Looking one up by call_id 404s for every live
    # call, which then also tears down the audio socket. Fall back to the turns
    # actually processed.
    #
    # Scripted calls still prefer the seed file: `live.history` holds only the
    # customer turns the pipeline ran on, whereas the file has the agent side
    # too, and the summary is better for having both.
    try:
        transcript = _turns(_load_transcript(call_id))
    except HTTPException:
        transcript = list(live.history)

    if not transcript:
        raise HTTPException(400, "no transcript to summarise")

    orch = get_orchestrator()

    with session_scope() as s:
        crm = get_crm_snapshot(s, live.customer_id)
        dnc = is_do_not_call(s, live.customer_id)

    result = orch.finalise_call(
        call_id=call_id,
        transcript=transcript,
        intents_seen=live.intents_seen,
        max_dropoff_risk=live.max_dropoff_risk,
        meter=live.meter,
        consent_ack=live.consent_ack,
        crm=crm,
        do_not_call=dnc,
        sentiments_seen=live.sentiments_seen,
        buying_signals=live.buying_signals,
    )

    payload = json.loads(result.model_dump_json())
    live.finalised = payload

    with session_scope() as s:
        call = s.get(Call, call_id)
        if call:
            call.ended_at = datetime.now(timezone.utc)
            call.disposition = result.crm.disposition.value
            call.summary = result.crm.summary
            call.dropoff_reason = result.crm.dropoff_reason
            call.crm_patch_json = json.dumps(result.crm.crm_patch)
            call.followup_json = json.dumps(
                result.crm.followup_draft.model_dump()
                if result.crm.followup_draft
                else {}
            )
            call.send_status = result.crm.send_status.value
            call.guardrail_trace_json = json.dumps(
                [json.loads(c.model_dump_json()) for c in result.guardrail.checks]
            )
            call.cost_usd = result.ledger.total_usd
            # A live call's transcript exists only in memory until now.
            call.transcript_json = json.dumps(
                [json.loads(t.model_dump_json()) for t in transcript]
            )

    payload["frontier_usd"] = live.meter.frontier_baseline_usd()
    return payload


class ApprovalBody(BaseModel):
    approver_id: str
    edited_summary: Optional[str] = None
    edited_followup_body: Optional[str] = None
    decision: str = "approve"  # approve | reject


@app.post("/api/calls/{call_id}/approve")
def approve(call_id: str, body: ApprovalBody) -> dict:
    """The human oversight gate.

    This is the ONLY path that writes an AI-proposed patch to a customer record
    or marks a follow-up sendable, and it requires a named approver. No agent
    can call it. An agent that decided a message was fine to send would still be
    stuck at `pending_agent_approval`.
    """
    if not body.approver_id.strip():
        raise HTTPException(400, "approver_id is required — approvals are attributed")

    with session_scope() as s:
        call = s.get(Call, call_id)
        if not call:
            raise HTTPException(404, "call not found")

        if body.decision == "reject":
            call.send_status = "rejected"
            call.approved_by = body.approver_id
            call.approved_at = datetime.now(timezone.utc)
            return {"call_id": call_id, "send_status": "rejected"}

        # Human edits win over the model's draft.
        if body.edited_summary is not None:
            call.summary = body.edited_summary
        if body.edited_followup_body is not None:
            fu = json.loads(call.followup_json or "{}")
            if fu:
                fu["body"] = body.edited_followup_body
                call.followup_json = json.dumps(fu)

        ok, message = apply_approved_patch(s, call_id, body.approver_id)
        if not ok:
            raise HTTPException(400, message)

        followup = json.loads(call.followup_json or "{}")
        if followup.get("body"):
            call.send_status = "sent"

        return {
            "call_id": call_id,
            "send_status": call.send_status,
            "approved_by": call.approved_by,
            "crm_patch_applied": json.loads(call.crm_patch_json or "{}"),
            "message": message,
        }


@app.get("/api/calls/{call_id}/ledger")
def ledger(call_id: str) -> dict:
    live = LIVE.get(call_id)
    if live is None:
        raise HTTPException(404, "call not started")
    return {
        "ledger": json.loads(live.meter.ledger().model_dump_json()),
        "frontier_usd": live.meter.frontier_baseline_usd(),
        "summary": live.meter.summary_line(),
    }


@app.get("/api/customers")
def list_customers(db: Session = Depends(get_session)) -> list[dict]:
    """Customers a live call can be opened against."""
    return [
        {
            "customer_id": c.customer_id,
            "name": c.name,
            "city": c.city,
            "kyc_status": c.kyc_status,
            "last_disposition": c.last_disposition,
            "do_not_call": c.do_not_call,
        }
        for c in db.query(Customer).order_by(Customer.customer_id).all()
    ]


@app.get("/api/customers/{customer_id}")
def get_customer(customer_id: str, db: Session = Depends(get_session)) -> dict:
    c = db.get(Customer, customer_id)
    if not c:
        raise HTTPException(404, "customer not found")
    return {
        "customer_id": c.customer_id,
        "name": c.name,
        "phone_masked": c.phone_masked,
        "city": c.city,
        "kyc_status": c.kyc_status,
        "kyc_last_step": c.kyc_last_step,
        "credit_limit_inr": c.credit_limit_inr,
        "last_disposition": c.last_disposition,
        "do_not_call": c.do_not_call,
        "interactions": [
            {"at": i.at.isoformat(), "note": i.note} for i in c.interactions
        ],
    }


# ---------------------------------------------------------------------------
# Whisper
# ---------------------------------------------------------------------------


@app.post("/api/transcribe")
async def transcribe(file: UploadFile = File(...)) -> dict:
    """Transcribe audio with Whisper on Groq. Pure STT, no pipeline.

    Same key and provider as everything else, billed per hour of audio rather
    than per token ($0.04/hr on whisper-large-v3-turbo — about $0.003 for a
    five-minute call).
    """
    audio = await file.read()
    if not audio:
        raise HTTPException(400, "empty upload")

    try:
        text, duration = await asyncio.to_thread(
            get_llm().transcribe_bytes, audio, file.filename or "audio.wav"
        )
    except Exception as e:  # noqa: BLE001 - surface provider errors to the UI
        raise HTTPException(502, f"transcription failed: {e}") from e

    from app.guardrails.pii import redact

    redaction = redact(text)
    return {
        "text": redaction.text,
        "pii_redacted": redaction.found,
        "audio_seconds": duration,
        "model": settings.model_stt,
        "usd": round(price_audio_seconds(settings.model_stt, duration), 8),
    }


@app.post("/api/live/{call_id}/audio-turn")
async def audio_turn(
    call_id: str,
    file: UploadFile = File(...),
    speaker: str = "customer",
) -> dict:
    """Transcribe an uploaded clip and run it through the full pipeline.

    The REST counterpart of the live-mic socket, for analysing a recorded call
    or for demoing the voice path without a working microphone. Same consent
    gate, same orchestrator, same guardrails.
    """
    live = LIVE.get(call_id)
    if live is None or not live.consent_ack:
        raise HTTPException(400, "call not started, or no consent on record")

    audio = await file.read()
    if not audio:
        raise HTTPException(400, "empty upload")

    llm = get_llm()
    try:
        text, duration = await asyncio.to_thread(
            llm.transcribe_bytes, audio, file.filename or "audio.wav"
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"transcription failed: {e}") from e

    live.meter.record_stt(
        "whisper",
        settings.model_stt,
        price_audio_seconds(settings.model_stt, duration),
        0.0,
    )

    if is_probably_hallucination(text, duration):
        raise HTTPException(
            422,
            f"No speech detected in that clip — Whisper returned {text!r}, which "
            "is silence filler rather than a real utterance.",
        )

    turn = TranscriptTurn(
        index=len(live.history),
        speaker=Speaker.AGENT if speaker == "agent" else Speaker.CUSTOMER,
        text=text,
        ts=float(len(live.history)),
    )

    with session_scope() as s:
        crm = get_crm_snapshot(s, live.customer_id)

    assist = await asyncio.to_thread(
        get_orchestrator().handle_turn,
        call_id=call_id,
        turn=turn,
        history=list(live.history),
        meter=live.meter,
        consent_ack=live.consent_ack,
        crm=crm,
    )
    live.history.append(turn)
    if assist.intent:
        live.observe(assist)

    payload = json.loads(assist.model_dump_json())
    live.assists.append(payload)
    return {
        "assist": payload,
        "audio_seconds": duration,
        "ledger": json.loads(live.meter.ledger().model_dump_json()),
        "frontier_usd": live.meter.frontier_baseline_usd(),
    }

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
import time
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import (
    Header,
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
from sqlalchemy import text as sa_text
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
from app.crm.backends import get_crm_backend
from app.crm.models import Call, CostRow, Customer
from app.llm.client import LLMUnavailable, get_llm, is_probably_hallucination
from app.llm.router import describe_routing
from app.orchestrator import ConsentNotGiven, get_orchestrator
from app.export import (
    CALL_COLUMNS,
    TRACE_COLUMNS,
    call_rows,
    to_csv,
    trace_rows,
)
from app.guardrails import rules
from app.integrations import brevo, firestore_sync, sheets
from app.security import (
    Principal,
    auth_enabled,
    get_principal,
    principal_for,
    requires,
)
from app.schemas import (
    CheckName,
    CheckResult,
    Intent,
    ModelTier,
    Sentiment,
    Speaker,
    TranscriptTurn,
)
from app.telemetry.cost import CostMeter

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



@app.exception_handler(LLMUnavailable)
def _llm_unavailable(request: Request, exc: LLMUnavailable) -> Response:
    """Report a provider outage as an outage, not as a crash.

    A Groq quota exhaustion used to surface in the dashboard as a bare "500
    Internal Server Error" with a customer on the line -- which tells the agent
    nothing about whether the fault was theirs, the customer's, the account's or
    the code's. 503 with a sentence they can act on is the whole fix.
    """
    return Response(
        content=json.dumps(
            {
                "detail": exc.human,
                "kind": exc.kind,
                "retry_after_s": exc.retry_after_s,
            }
        ),
        status_code=503,
        media_type="application/json",
        headers=(
            {"Retry-After": str(int(exc.retry_after_s))} if exc.retry_after_s else {}
        ),
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

        self.source: str = "browser"  # browser | scripted
        self.ended: bool = False

    def observe(self, assist: Any) -> None:
        """Accumulate per-turn signals the post-call summariser needs.

        One method rather than repeating the same four lines at each of the
        scripted, browser-mic and upload call sites -- which is how
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
def record_consent(call_id: str, body: ConsentBody,
    principal: Principal = Depends(requires("call"))) -> dict:
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

        # A seed scenario keeps its call_id across runs, so without this the
        # ledger accumulates: replaying `call-001` three times leaves three
        # runs' worth of rows under one id, and the exported trace reports a
        # single call costing triple. Live calls get a fresh uuid and never hit
        # this. Re-running a scenario replaces the previous run, which is what
        # "run it again" means to anyone watching.
        s.query(CostRow).filter(CostRow.call_id == call_id).delete()
        call.cost_usd = 0.0
        # Consent marks the start of *this* run. Without resetting these, a
        # replayed scenario keeps its original started_at and pairs it with
        # today's ended_at -- the exported CSV then reports a five-hour call.
        call.started_at = live.consent_at
        call.ended_at = None
        # A previous run's approval must not survive into this one. Leaving it
        # produced a row reading "approved_by Subhash" next to
        # "send_status pending_agent_approval" -- two fields disagreeing about
        # whether a human had signed anything.
        call.send_status = "pending_agent_approval"
        call.approved_by = None
        call.approved_at = None
        call.guardrail_trace_json = "[]"

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
    crm = get_crm_backend().read_snapshot(live.customer_id)

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
            except LLMUnavailable as e:
                # Do NOT break. The customer is still talking and the transcript
                # is still worth keeping; losing the socket as well would turn a
                # provider outage into a lost call.
                await ws.send_json(
                    {
                        "type": "llm_unavailable",
                        "kind": e.kind,
                        "message": e.human,
                        "retry_after_s": e.retry_after_s,
                    }
                )
                live.history.append(turn)
                continue

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
def live_start(
    body: LiveStartBody,
    db: Session = Depends(get_session),
    principal: Principal = Depends(requires("call")),
) -> dict:
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
    crm = get_crm_backend().read_snapshot(live.customer_id)

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
            except LLMUnavailable as e:
                # Do NOT break. The customer is still talking and the transcript
                # is still worth keeping; losing the socket as well would turn a
                # provider outage into a lost call.
                await ws.send_json(
                    {
                        "type": "llm_unavailable",
                        "kind": e.kind,
                        "message": e.human,
                        "retry_after_s": e.retry_after_s,
                    }
                )
                live.history.append(turn)
                continue

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


# ---------------------------------------------------------------------------
# Post-call
# ---------------------------------------------------------------------------


@app.post("/api/calls/{call_id}/finalise")
def finalise(call_id: str,
    principal: Principal = Depends(requires("call"))) -> dict:
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

    # Through the configured connector, not straight to SQLite. Swapping in a
    # real CRM is a .env change, not an edit here.
    crm = get_crm_backend().read_snapshot(live.customer_id)
    dnc = get_crm_backend().is_do_not_call(live.customer_id)

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
    # Mirrored after the local write, never before: SQLite is the record, and a
    # cloud outage must not lose a summarised call.
    payload["mirrored"] = _publish(call_id)
    return payload


class ApprovalBody(BaseModel):
    """Note what is absent: the approver's name.

    It used to be a field here, which meant the identity on a customer-record
    write was whatever the caller typed. It now comes from the credential --
    see `app/security.py`. A gate that lets the caller name themselves is a
    convention, not a control.
    """

    edited_summary: Optional[str] = None
    edited_followup_body: Optional[str] = None
    decision: str = "approve"  # approve | reject


def _blocking_checks(call: Call) -> list[dict]:
    """The post-call checks that did not pass, from the persisted trace."""
    try:
        trace = json.loads(call.guardrail_trace_json or "[]")
    except json.JSONDecodeError:
        return []
    return [c for c in trace if isinstance(c, dict) and not c.get("passed", True)]


@app.post("/api/calls/{call_id}/approve")
def approve(
    call_id: str,
    body: ApprovalBody,
    principal: Principal = Depends(requires("approve")),
) -> dict:
    """The human oversight gate.

    This is the ONLY path that writes an AI-proposed patch to a customer record
    or marks a follow-up sendable, and it requires a named approver. No agent
    can call it. An agent that decided a message was fine to send would still be
    stuck at `pending_agent_approval`.

    It also refuses to send a message its own guardrail rejected. That sounds
    obvious; it was not true. The post-call check ran, correctly caught a draft
    claiming Pay-in-3 was entirely free with no mention of the late or bounce
    fee, wrote the verdict to the trace — and this endpoint never read it. The
    message went out marked `sent`. A guardrail nothing consumes is decoration,
    so the verdict is now load-bearing here rather than only rendered.

    The way past a block is to rewrite the message, not to click again. An edit
    is re-checked against the deterministic rules and the override is recorded
    in the trace under the approver's name, because the accountable act is a
    human choosing to send different words, not a human dismissing a warning.
    """
    with session_scope() as s:
        call = s.get(Call, call_id)
        if not call:
            raise HTTPException(404, "call not found")

        if body.decision == "reject":
            call.send_status = "rejected"
            call.approved_by = principal.audit_name
            call.approved_at = datetime.now(timezone.utc)
            return {"call_id": call_id, "send_status": "rejected"}

        original = json.loads(call.followup_json or "{}")
        original_body = (original.get("body") or "").strip()
        edited_body = (
            body.edited_followup_body.strip()
            if body.edited_followup_body is not None
            else None
        )
        rewritten = edited_body is not None and edited_body != original_body

        # -- the gate -----------------------------------------------------
        failures = _blocking_checks(call)
        overrides: list[CheckResult] = []
        if failures and original_body:
            if not rewritten:
                names = ", ".join(str(f.get("name", "check")) for f in failures)
                detail = next(
                    (f.get("detail") for f in failures if f.get("detail")), ""
                )
                raise HTTPException(
                    400,
                    f"Blocked by {names}: {detail} Rewrite the message before "
                    f"sending it, or discard it.",
                )

            # A rewrite still has to clear the checks that are code, not
            # judgement — a human is allowed to disagree with the model about
            # tone, never to send a claim that an action already happened.
            recheck = rules.check_no_fabricated_actions(edited_body or "")
            if not recheck.passed:
                raise HTTPException(400, f"Your edit still fails: {recheck.detail}")

            overrides = [
                CheckResult(
                    name=CheckName(f["name"]),
                    passed=True,
                    enforced_by="code",
                    detail=(
                        f"Draft was blocked; {principal.audit_name} rewrote the "
                        f"message and sent their own wording."
                    ),
                )
                for f in failures
                if f.get("name") in {c.value for c in CheckName}
            ]

        # Human edits win over the model's draft.
        if body.edited_summary is not None:
            call.summary = body.edited_summary
        if edited_body is not None and original:
            original["body"] = edited_body
            call.followup_json = json.dumps(original)

        ok, message = apply_approved_patch(s, call_id, principal.audit_name)
        if not ok:
            raise HTTPException(400, message)

        if overrides:
            trace = json.loads(call.guardrail_trace_json or "[]")
            trace.extend(json.loads(c.model_dump_json()) for c in overrides)
            call.guardrail_trace_json = json.dumps(trace)

        # --- delivery -----------------------------------------------------
        # Reached only after the gate above: a blocked draft was refused, a
        # rewrite was re-checked, and the approver came from their credential.
        followup = json.loads(call.followup_json or "{}")
        delivery: dict[str, Any] = {"attempted": False}
        if followup.get("body"):
            # A human has said yes. That is true regardless of what the email
            # provider does next, so record it before attempting delivery.
            call.send_status = "approved"

            if brevo.enabled():
                customer = s.get(Customer, call.customer_id)
                res = brevo.send_email(
                    to_email=getattr(customer, "email", "") or "",
                    to_name=getattr(customer, "name", "") or "",
                    subject=followup.get("subject") or "",
                    body=followup["body"],
                    approved_by=principal.audit_name,
                    call_id=call_id,
                )
                delivery = {
                    "attempted": True,
                    "ok": res.ok,
                    "recipient": res.recipient,
                    "redirected": res.redirected,
                    "message_id": res.message_id,
                    "detail": res.detail,
                }
                # `sent` now means a provider accepted it. On failure the call
                # stays `approved` -- writing `sent` because we tried would make
                # the state machine lie in the only direction that matters, and
                # the follow-up would be silently lost.
                if res.ok:
                    call.send_status = "sent"
            else:
                # No provider configured. The draft is approved and queued;
                # calling that "sent" is what this whole change exists to stop.
                delivery = {"attempted": False, "detail": "Brevo not configured."}

        result = {
            "call_id": call_id,
            "send_status": call.send_status,
            "approved_by": call.approved_by,
            "crm_patch_applied": json.loads(call.crm_patch_json or "{}"),
            "overrode": [c.name.value for c in overrides],
            "delivery": delivery,
            "message": message,
        }
        customer = s.get(Customer, call.customer_id)
        snapshot = (
            {
                c.name: getattr(customer, c.name)
                for c in Customer.__table__.columns
            }
            if customer
            else None
        )

    # Outside the session: the approval is committed before anything is
    # mirrored. An approval that succeeded locally is not un-done by a network
    # failure, and the status endpoint reports the mirror lag instead.
    if snapshot:
        snapshot = {
            k: (v.isoformat() if hasattr(v, "isoformat") else v)
            for k, v in snapshot.items()
        }
        firestore_sync.sync_customer(snapshot)
    result["mirrored"] = _publish(call_id)
    return result


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

    crm = get_crm_backend().read_snapshot(live.customer_id)

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


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------


@app.get("/api/export/calls.csv")
def export_calls(call_id: Optional[str] = None) -> Response:
    """One row per call: what happened, what was written, who signed it off.

    The automation boundary is legible in the columns. Everything up to
    `followup_body` is produced without a human; `send_status`, `approved_by`
    and `approved_at` are the only fields no agent can set.
    """
    with session_scope() as s:
        body = to_csv(CALL_COLUMNS, call_rows(s, call_id))
    name = f"sahai-calls-{call_id or 'all'}.csv"
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.get("/api/export/trace.csv")
def export_trace(call_id: Optional[str] = None) -> Response:
    """One row per pipeline stage, in order, with what it cost.

    This is the end-to-end trace: every stage a call passed through, the tier
    and model it used, a one-line summary of what went in and came out, real
    token counts from the API response, and the priced cost. Summing `usd` for
    a call_id reproduces the cost-per-call figure exactly -- nothing here is
    estimated.
    """
    with session_scope() as s:
        body = to_csv(TRACE_COLUMNS, trace_rows(s, call_id))
    name = f"sahai-trace-{call_id or 'all'}.csv"
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


def _publish(call_id: str) -> dict[str, Any]:
    """Push one call to every configured destination, from one set of rows.

    The rows come from `app.export`, the same builders the CSV endpoints use, so
    the file, the Firestore document and the spreadsheet row can never disagree
    about what a call cost or whether a human signed it.

    Best-effort by design: a Firestore outage or a revoked Sheets token must not
    fail a call that has already been summarised and written locally. SQLite has
    the truth; these are mirrors of it.
    """
    with session_scope() as s:
        call = list(call_rows(s, call_id))
        stages = list(trace_rows(s, call_id))
    if not call:
        return {"firestore": False, "sheets": None}
    return {
        "firestore": firestore_sync.sync_call(call[0], stages),
        "sheets": sheets.push_call(call[0], stages),
    }


@app.get("/api/integrations/status")
def integrations_status() -> dict:
    """What is configured, what is reachable, and what has failed.

    Exposed so "the data is in Firestore" is checkable rather than asserted --
    the same reason the routing policy is served at /api/policy.
    """
    return {
        "crm": get_crm_backend().describe(),
        "brevo": brevo.status(),
        "firestore": firestore_sync.status(),
        "sheets": sheets.status(),
    }


@app.post("/api/integrations/sync")
def integrations_sync(call_id: Optional[str] = None,
    principal: Principal = Depends(requires("integrate"))) -> dict:
    """Backfill. Push one call, or every call, to Firestore and Sheets.

    A full sync writes Sheets in one pass rather than per call. Upserting one
    row at a time needs several reads of the sheet each time, which for thirty
    calls exceeds Google's 60-reads-per-minute limit and leaves the tab half
    written -- indistinguishable, to anyone looking at it, from missing data.
    """
    if call_id:
        return {call_id: _publish(call_id)}

    with session_scope() as s:
        calls = list(call_rows(s))
        stages = list(trace_rows(s))

    # Firestore has no equivalent limit and is cheap per document, so it stays
    # per call and keeps its subcollection layout.
    fs = {c["call_id"]: firestore_sync.sync_call(c, [
        st for st in stages if st["call_id"] == c["call_id"]
    ]) for c in calls}

    url = sheets.replace_all(calls, stages)
    return {
        "calls": len(calls),
        "stages": len(stages),
        "firestore_ok": sum(1 for v in fs.values() if v),
        "sheets": url,
    }


@app.get("/api/me")
def whoami(x_api_key: Optional[str] = Header(None)) -> dict:
    """Who the caller is, and what they may do.

    The dashboard reads this to decide whether to ask an agent to type their
    name at the approval gate or to show the identity it will sign with. A UI
    that asks for a name it is going to ignore teaches the wrong thing about
    where the authority comes from.
    """
    # Deliberately not behind `get_principal`. This is the endpoint the
    # dashboard calls to find out whether to show a sign-in screen, so 401-ing
    # an anonymous caller makes it impossible to distinguish "no credential"
    # from "server unreachable" — and the dashboard then renders itself as if
    # signed in. Reporting "you are nobody" is the answer, not an error.
    principal = principal_for(x_api_key) or Principal(
        key_id="-", name="Not signed in", role="viewer", authenticated=False
    )
    return {
        "name": principal.name,
        "role": principal.role,
        "authenticated": principal.authenticated,
        "auth_enabled": auth_enabled(),
        "can": {
            a: principal.can(a) for a in ("read", "call", "approve", "integrate")
        },
    }


# ---------------------------------------------------------------------------
# Operations: correlation, liveness, readiness, and the built dashboard
# ---------------------------------------------------------------------------


@app.middleware("http")
async def _request_id(request: Request, call_next):
    """Stamp every request and response with an id, and time it.

    A call fans out across six agents and two mirrors; without a correlation id
    the only way to tie a slow turn to its log lines is by timestamp, which
    stops working the moment two agents are on the phone at once.
    """
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    response.headers["X-Response-Time-ms"] = f"{(time.perf_counter() - started) * 1000:.1f}"
    return response


@app.get("/healthz")
def healthz() -> dict:
    """Liveness: is the process up. Deliberately checks nothing else.

    A liveness probe that touches a dependency restarts a healthy container
    because a database blipped, which turns a brief outage into a crash loop.
    """
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> Response:
    """Readiness: can this instance actually serve a call.

    Checks the two things whose absence makes every request fail rather than
    degrade — the database and the knowledge-base index. The model provider is
    *not* checked: a quota failure is reported per request as a 503 with an
    explanation, and pulling the instance out of rotation for it would take the
    dashboard down as well.
    """
    checks: dict[str, Any] = {}
    ok = True

    try:
        with session_scope() as s:
            s.execute(sa_text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as e:  # noqa: BLE001
        checks["database"] = f"{type(e).__name__}"
        ok = False

    try:
        from app.rag.retriever import get_retriever

        checks["knowledge_base"] = f"{len(get_retriever().records)} chunks"
    except Exception as e:  # noqa: BLE001
        checks["knowledge_base"] = f"{type(e).__name__} — run app.rag.ingest"
        ok = False

    checks["auth"] = "enforced" if auth_enabled() else "open"
    checks["crm"] = get_crm_backend().describe()["connector"]
    return Response(
        content=json.dumps({"ready": ok, "checks": checks}),
        status_code=200 if ok else 503,
        media_type="application/json",
    )


# The container build drops the compiled dashboard here. Mounted last so it
# cannot shadow an API route, and skipped entirely in development where Vite
# serves the frontend itself.
_STATIC = BACKEND_DIR / "static"
if _STATIC.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(_STATIC), html=True), name="dashboard")

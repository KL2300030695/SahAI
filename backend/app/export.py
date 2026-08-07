"""
CSV export: the pipeline's output as a file you can open, diff, or drag into
Google Sheets.

Two exports, because two different questions get asked about this system:

* `calls.csv`  -- one row per call. What happened, what was written, who signed
  it off. This is the CRM output a sales manager would actually read.
* `trace.csv`  -- one row per pipeline stage. Timestamp, stage, model, tier,
  what went in, what came out, tokens, cost, latency. This is the end-to-end
  trace, and it is the artefact that makes the cost-per-call number checkable
  rather than asserted.

Both are generated from what was persisted during the call, not recomputed
afterwards, so an exported row cannot disagree with what the system did.

CSV rather than a live Google Sheets write, deliberately: no service-account
credential to leak, no network dependency mid-demo, and a file that opens in
Sheets by dragging it in. The row schemas below are the ones a Sheets appender
would use, so that remains a small adapter rather than a parallel path.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Iterable, Iterator, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.crm.models import Call, CostRow, Customer

# --- calls.csv -------------------------------------------------------------

CALL_COLUMNS = [
    "call_id",
    "customer_id",
    "customer_name",
    "city",
    "agent_name",
    "started_at",
    "ended_at",
    "duration_s",
    "consent_ack",
    "disposition",
    "drop_off",
    "drop_off_reason",
    "summary",
    "crm_patch",
    "followup_channel",
    "followup_body",
    # The automation boundary, as data: everything above is written without a
    # human; nothing below moves without one.
    "send_status",
    "approved_by",
    "approved_at",
    "guardrail_passed",
    "guardrail_failed_checks",
    "turns",
    "cost_usd",
]


def _fmt(dt) -> str:
    return dt.isoformat(timespec="seconds") if dt else ""


def _call_row(s: Session, call: Call) -> dict[str, object]:
    cust = s.get(Customer, call.customer_id)
    followup = json.loads(call.followup_json or "{}")
    try:
        trace = json.loads(call.guardrail_trace_json or "[]")
    except json.JSONDecodeError:
        trace = []
    failed = [c.get("name", "") for c in trace if isinstance(c, dict) and not c.get("passed", True)]

    duration = ""
    if call.started_at and call.ended_at:
        duration = str(int((call.ended_at - call.started_at).total_seconds()))

    turns = 0
    try:
        turns = len(json.loads(call.transcript_json or "[]"))
    except json.JSONDecodeError:
        pass

    return {
        "call_id": call.call_id,
        "customer_id": call.customer_id,
        "customer_name": cust.name if cust else "",
        "city": getattr(cust, "city", "") or "",
        "agent_name": call.agent_name or "",
        "started_at": _fmt(call.started_at),
        "ended_at": _fmt(call.ended_at),
        "duration_s": duration,
        "consent_ack": call.consent_ack,
        "disposition": call.disposition or "",
        # The brief asks for an explicit boolean rather than making a reader
        # infer drop-off from the presence of a reason string.
        "drop_off": bool(call.dropoff_reason),
        "drop_off_reason": call.dropoff_reason or "",
        "summary": call.summary or "",
        "crm_patch": call.crm_patch_json or "{}",
        "followup_channel": followup.get("channel", ""),
        "followup_body": followup.get("body", ""),
        "send_status": call.send_status,
        "approved_by": call.approved_by or "",
        "approved_at": _fmt(call.approved_at),
        "guardrail_passed": not failed,
        "guardrail_failed_checks": ";".join(failed),
        "turns": turns,
        "cost_usd": f"{call.cost_usd:.8f}",
    }


def call_rows(s: Session, call_id: Optional[str] = None) -> Iterator[dict[str, object]]:
    stmt = select(Call).order_by(Call.started_at.desc())
    if call_id:
        stmt = stmt.where(Call.call_id == call_id)
    for call in s.execute(stmt).scalars():
        yield _call_row(s, call)


# --- trace.csv -------------------------------------------------------------

TRACE_COLUMNS = [
    "at",
    "call_id",
    "turn_index",
    "stage",
    "tier",
    "model",
    "detail",
    "prompt_tokens",
    "completion_tokens",
    "usd",
    "latency_ms",
    "escalation_trigger",
]


def trace_rows(s: Session, call_id: Optional[str] = None) -> Iterator[dict[str, object]]:
    stmt = select(CostRow).order_by(CostRow.at.asc(), CostRow.id.asc())
    if call_id:
        stmt = stmt.where(CostRow.call_id == call_id)
    for row in s.execute(stmt).scalars():
        yield {
            "at": _fmt(row.at),
            "call_id": row.call_id,
            "turn_index": "" if row.turn_index is None else row.turn_index,
            "stage": row.agent,
            "tier": row.tier,
            "model": row.model,
            "detail": row.note or "",
            "prompt_tokens": row.prompt_tokens,
            "completion_tokens": row.completion_tokens,
            "usd": f"{row.usd:.8f}",
            "latency_ms": f"{row.latency_ms:.1f}",
            "escalation_trigger": row.escalation_trigger or "",
        }


# --- rendering -------------------------------------------------------------


def to_csv(columns: list[str], rows: Iterable[dict[str, object]]) -> str:
    """Render rows as CSV text.

    `\\r\\n` and QUOTE_MINIMAL are csv module defaults and are what Excel and
    Sheets expect. Summaries and follow-up bodies contain commas and newlines
    routinely, so quoting is doing real work here, not ceremony.
    """
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    w.writeheader()
    for row in rows:
        w.writerow(row)
    return buf.getvalue()

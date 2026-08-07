"""
CSV export.

The export is the artefact a judge or a sales manager actually opens, so the
tests are about the two things that would make it misleading rather than about
the csv module: that the automation boundary is legible in the columns, and
that a summary containing commas and newlines survives the round trip.
"""

from __future__ import annotations

import csv
import io
import json

from app.crm.db import session_scope
from app.crm.models import Call, Customer
from app.export import (
    CALL_COLUMNS,
    TRACE_COLUMNS,
    call_rows,
    to_csv,
    trace_rows,
)

CALL_ID = "test-export-row"
CUSTOMER_ID = "TEST-CUST-EXPORT"

NASTY = 'Customer said "no, thanks", then:\nasked about fees, KYC, and the 3-month split.'


def _seed():
    with session_scope() as s:
        if not s.get(Customer, CUSTOMER_ID):
            s.add(Customer(customer_id=CUSTOMER_ID, name="Export Test", city="Pune"))
        old = s.get(Call, CALL_ID)
        if old:
            s.delete(old)
        s.flush()
        s.add(
            Call(
                call_id=CALL_ID,
                customer_id=CUSTOMER_ID,
                agent_name="tester",
                consent_ack=True,
                disposition="dropped",
                summary=NASTY,
                dropoff_reason="Uncomfortable sharing Aadhaar",
                crm_patch_json=json.dumps({"kyc_last_step": 3}),
                followup_json=json.dumps({"channel": "sms", "body": "Hi, one, two"}),
                guardrail_trace_json=json.dumps(
                    [{"name": "grounding", "passed": False, "enforced_by": "code"}]
                ),
                send_status="pending_agent_approval",
                cost_usd=0.00321,
            )
        )


def _cleanup():
    with session_scope() as s:
        for model, key in ((Call, CALL_ID), (Customer, CUSTOMER_ID)):
            row = s.get(model, key)
            if row:
                s.delete(row)


def _row() -> dict:
    _seed()
    try:
        with session_scope() as s:
            body = to_csv(CALL_COLUMNS, call_rows(s, CALL_ID))
    finally:
        _cleanup()
    return list(csv.DictReader(io.StringIO(body)))[0]


def test_commas_and_newlines_in_a_summary_survive():
    """Summaries routinely contain both. Quoting is load-bearing, not ceremony."""
    assert _row()["summary"] == NASTY


def test_drop_off_is_an_explicit_boolean():
    """Not left for a reader to infer from whether a reason string is present."""
    r = _row()
    assert r["drop_off"] == "True"
    assert r["drop_off_reason"] == "Uncomfortable sharing Aadhaar"


def test_the_automation_boundary_is_visible_in_the_row():
    """A pending call must never look sent, and must name nobody."""
    r = _row()
    assert r["send_status"] == "pending_agent_approval"
    assert r["approved_by"] == ""
    assert r["approved_at"] == ""
    assert r["guardrail_passed"] == "False"
    assert r["guardrail_failed_checks"] == "grounding"


def test_headers_are_stable():
    """A Sheets appender would bind to these; renaming one silently breaks it."""
    with session_scope() as s:
        calls = to_csv(CALL_COLUMNS, call_rows(s, "nope"))
        trace = to_csv(TRACE_COLUMNS, trace_rows(s, "nope"))
    assert calls.splitlines()[0].split(",")[:4] == [
        "call_id", "customer_id", "customer_name", "city",
    ]
    assert trace.splitlines()[0].split(",")[:6] == [
        "at", "call_id", "turn_index", "stage", "tier", "model",
    ]

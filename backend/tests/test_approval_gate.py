"""
The approval gate — the only path that writes to a customer record.

These exist because of a real failure. The post-call self-check caught a
follow-up claiming Pay-in-3 was entirely free with no mention of the late or
bounce fee, wrote a failing verdict to the trace, and the approve endpoint
never read it. The message was written to the record and marked `sent`, with
the check that rejected it rendered on the same screen.

A guardrail nothing consumes is decoration, so these tests assert on the
consumption rather than on the verdict.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.crm.db import session_scope
from app.crm.models import Call, Customer
from app.main import app

CALL_ID = "test-approval-gate"
CUSTOMER_ID = "TEST-CUST-GATE"

BLOCKED_TRACE = [
    {
        "name": "goal_alignment",
        "passed": False,
        "detail": (
            "It claims the Pay-in-3 plan is entirely free without noting "
            "possible late or bounce fees, violating rule to disclose fees."
        ),
        "enforced_by": "llm",
        "severity": "block",
    }
]

BAD_DRAFT = "Great news — Pay-in-3 is completely free, no charges at all!"


@pytest.fixture
def client():
    with session_scope() as s:
        if not s.get(Customer, CUSTOMER_ID):
            s.add(Customer(customer_id=CUSTOMER_ID, name="Gate Test", city="Pune"))
    yield TestClient(app)
    with session_scope() as s:
        call = s.get(Call, CALL_ID)
        if call:
            s.delete(call)
        cust = s.get(Customer, CUSTOMER_ID)
        if cust:
            s.delete(cust)


def _seed(trace: list, draft: str = BAD_DRAFT) -> None:
    with session_scope() as s:
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
                summary="Customer asked about fees.",
                crm_patch_json="{}",
                followup_json=json.dumps({"channel": "email", "body": draft}),
                guardrail_trace_json=json.dumps(trace),
                send_status="pending_agent_approval",
            )
        )


def _status() -> str:
    with session_scope() as s:
        return s.get(Call, CALL_ID).send_status


def test_a_blocked_message_cannot_be_sent_unchanged(client):
    """The exact failure from the screenshot: blocked, yet marked sent."""
    _seed(BLOCKED_TRACE)
    r = client.post(
        f"/api/calls/{CALL_ID}/approve",
        json={"approver_id": "subhash", "edited_followup_body": BAD_DRAFT},
    )
    assert r.status_code == 400
    assert "goal_alignment" in r.json()["detail"]
    assert _status() == "pending_agent_approval"


def test_clicking_approve_without_touching_the_draft_is_also_refused(client):
    """No edit field at all is the same as an unchanged one."""
    _seed(BLOCKED_TRACE)
    r = client.post(
        f"/api/calls/{CALL_ID}/approve", json={"approver_id": "subhash"}
    )
    assert r.status_code == 400
    assert _status() == "pending_agent_approval"


def test_a_rewrite_clears_the_block_and_is_attributed(client):
    """The way past a block is different words, under a name."""
    _seed(BLOCKED_TRACE)
    r = client.post(
        f"/api/calls/{CALL_ID}/approve",
        json={
            "approver_id": "subhash",
            "edited_followup_body": (
                "Pay-in-3 splits your purchase into three instalments at no "
                "extra cost when you pay on schedule. I'll send the fee "
                "details across so you have them in writing."
            ),
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["send_status"] == "sent"
    assert "goal_alignment" in r.json()["overrode"]

    # The override is recorded in the audit trace, not swallowed.
    with session_scope() as s:
        trace = json.loads(s.get(Call, CALL_ID).guardrail_trace_json)
    assert any("subhash" in (c.get("detail") or "") for c in trace)


def test_a_rewrite_still_cannot_claim_a_completed_action(client):
    """Deterministic checks survive the human override.

    A human may disagree with the model about tone. Nobody may tell a customer
    something was done that was not — that is the one failure with no recovery.
    """
    _seed(BLOCKED_TRACE)
    r = client.post(
        f"/api/calls/{CALL_ID}/approve",
        json={
            "approver_id": "subhash",
            "edited_followup_body": "I've already sent the fee schedule to your inbox.",
        },
    )
    assert r.status_code == 400
    assert "still fails" in r.json()["detail"]
    assert _status() == "pending_agent_approval"


def test_a_clean_call_still_sends_normally(client):
    """The gate must not become a wall — a passing call goes through untouched."""
    _seed([{"name": "grounding", "passed": True, "enforced_by": "code"}])
    r = client.post(
        f"/api/calls/{CALL_ID}/approve",
        json={"approver_id": "subhash"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["send_status"] == "sent"
    assert r.json()["overrode"] == []


def test_rejecting_writes_nothing(client):
    _seed(BLOCKED_TRACE)
    r = client.post(
        f"/api/calls/{CALL_ID}/approve",
        json={"approver_id": "subhash", "decision": "reject"},
    )
    assert r.status_code == 200
    assert r.json()["send_status"] == "rejected"

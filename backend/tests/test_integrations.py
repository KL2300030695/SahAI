"""
Firestore and Sheets mirrors.

Two properties matter more than whether a document lands, and both are tested
with fakes so the suite stays offline:

1. **A mirror never fails a call.** An agent mid-conversation does not care that
   a cloud store is unreachable, and failing the turn would be a far worse
   outcome than a stale document.
2. **Unconfigured is a supported state.** Anyone cloning this repo with no
   Google setup must get identical behaviour, because that is what a judge does.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.crm.db import session_scope
from app.crm.models import Call, Customer
from app.export import CALL_COLUMNS, TRACE_COLUMNS
from app.integrations import firestore_sync, sheets
from app.main import app

CALL_ID = "test-integrations"
CUSTOMER_ID = "TEST-CUST-INT"


@pytest.fixture(autouse=True)
def _clean():
    firestore_sync.reset_for_tests()
    sheets.reset_for_tests()
    get_settings.cache_clear()
    yield
    firestore_sync.reset_for_tests()
    sheets.reset_for_tests()
    get_settings.cache_clear()
    with session_scope() as s:
        for model, key in ((Call, CALL_ID), (Customer, CUSTOMER_ID)):
            row = s.get(model, key)
            if row:
                s.delete(row)


def _seed():
    with session_scope() as s:
        if not s.get(Customer, CUSTOMER_ID):
            s.add(Customer(customer_id=CUSTOMER_ID, name="Int Test", city="Pune"))
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
                summary="Asked about fees.",
                crm_patch_json=json.dumps({"kyc_last_step": 3}),
                followup_json=json.dumps({"channel": "sms", "body": "Hi"}),
                guardrail_trace_json="[]",
                send_status="pending_agent_approval",
            )
        )


# --- unconfigured is a supported state -------------------------------------
#
# These force the unconfigured state rather than reading whatever .env happens
# to hold. They passed on a laptop with no credentials and started failing the
# moment a real service-account key was added -- which means they had been
# testing the environment, not the code. Worse, one of them would then have
# written a document to the live project on every test run.


@pytest.fixture
def unconfigured(monkeypatch):
    monkeypatch.setattr(firestore_sync, "enabled", lambda: False)
    monkeypatch.setattr(sheets, "enabled", lambda: False)
    # A hard stop: if a guard is ever missed, the test fails loudly instead of
    # silently reaching Google.
    monkeypatch.setattr(
        firestore_sync,
        "_get_client",
        lambda: pytest.fail("touched Firestore while unconfigured"),
    )
    monkeypatch.setattr(
        sheets, "_get_client", lambda: pytest.fail("touched Sheets while unconfigured")
    )


def test_syncing_while_unconfigured_is_a_no_op_not_an_error(unconfigured):
    assert firestore_sync.sync_call({"call_id": "x"}, []) is False
    assert firestore_sync.sync_customer({"customer_id": "x"}) is False
    assert sheets.push_call({"call_id": "x"}, []) is None
    assert firestore_sync.status()["failures"] == 0


def test_status_never_claims_ready_without_the_prerequisites():
    """`ready` is a one-way promise, not an equality.

    It may legitimately be False while the flags look fine -- the conftest
    disables mirrors for the whole suite, which is exactly that case. What must
    never happen is the reverse: reporting ready with no credentials, which
    would send someone hunting for missing documents that were never sent.
    """
    body = TestClient(app).get("/api/integrations/status").json()
    for name in ("firestore", "sheets"):
        st = body[name]
        if st["ready"]:
            assert st["enabled"] and st["credentials_found"]
            assert st.get("sheet_id_set", True)


def test_finalise_and_approve_work_untouched_with_no_google_setup(unconfigured):
    """The path a judge takes: clone, run, never configure any of this."""
    _seed()
    r = TestClient(app).post(
        f"/api/calls/{CALL_ID}/approve",
        json={"approver_id": "tester"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["send_status"] == "sent"
    assert r.json()["mirrored"] == {"firestore": False, "sheets": None}


# --- a broken mirror must not break a call ---------------------------------


def test_a_firestore_outage_is_counted_not_raised(monkeypatch):
    monkeypatch.setattr(firestore_sync, "enabled", lambda: True)
    monkeypatch.setattr(
        firestore_sync,
        "_get_client",
        lambda: (_ for _ in ()).throw(ConnectionError("network is down")),
    )
    assert firestore_sync.sync_call({"call_id": CALL_ID}, []) is False
    st = firestore_sync.status()
    assert st["failures"] == 1
    assert "network is down" in st["last_error"]


def test_a_sheets_outage_is_counted_not_raised(monkeypatch):
    monkeypatch.setattr(sheets, "enabled", lambda: True)
    monkeypatch.setattr(
        sheets, "_sheet", lambda: (_ for _ in ()).throw(RuntimeError("token revoked"))
    )
    assert sheets.push_call({"call_id": CALL_ID}, []) is None
    assert sheets.status()["failures"] == 1


def test_approval_still_succeeds_when_every_mirror_is_down(monkeypatch):
    """The property that matters: the local write is authoritative."""
    _seed()
    monkeypatch.setattr(firestore_sync, "enabled", lambda: True)
    monkeypatch.setattr(
        firestore_sync,
        "_get_client",
        lambda: (_ for _ in ()).throw(ConnectionError("down")),
    )
    monkeypatch.setattr(sheets, "enabled", lambda: True)
    monkeypatch.setattr(
        sheets, "_sheet", lambda: (_ for _ in ()).throw(RuntimeError("down"))
    )

    r = TestClient(app).post(
        f"/api/calls/{CALL_ID}/approve", json={"approver_id": "tester"}
    )
    assert r.status_code == 200, r.text
    with session_scope() as s:
        assert s.get(Call, CALL_ID).send_status == "sent"


# --- both destinations describe the call identically -----------------------


def test_firestore_and_sheets_receive_the_same_rows(monkeypatch):
    """One row builder feeds both, so they cannot drift apart."""
    _seed()
    seen: dict[str, object] = {}

    monkeypatch.setattr(firestore_sync, "enabled", lambda: True)
    monkeypatch.setattr(
        firestore_sync,
        "sync_call",
        lambda call, stages: seen.__setitem__("fs", (call, list(stages))) or True,
    )
    monkeypatch.setattr(sheets, "enabled", lambda: True)
    monkeypatch.setattr(
        sheets,
        "push_call",
        lambda call, stages: seen.__setitem__("sh", (call, list(stages))) or "url",
    )

    TestClient(app).post(
        f"/api/calls/{CALL_ID}/approve", json={"approver_id": "tester"}
    )
    assert seen["fs"][0] == seen["sh"][0]
    assert set(seen["fs"][0]) == set(CALL_COLUMNS)


def test_stage_rows_carry_the_documented_trace_columns(monkeypatch):
    _seed()
    captured: list[dict] = []
    monkeypatch.setattr(firestore_sync, "enabled", lambda: True)
    monkeypatch.setattr(
        firestore_sync,
        "sync_call",
        lambda call, stages: captured.extend(stages) or True,
    )
    TestClient(app).post(
        f"/api/calls/{CALL_ID}/approve", json={"approver_id": "tester"}
    )
    for row in captured:
        assert set(row) == set(TRACE_COLUMNS)

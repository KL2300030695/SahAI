"""
Access control.

The suite runs unauthenticated by default (see conftest), so these turn
enforcement on deliberately. That split is the point: everything else must
behave identically whether or not a deployment has auth configured, and *this*
file is where the difference is pinned.

The claim being tested is narrow and load-bearing: the identity on a customer
record comes from the credential. Before this existed, `approver_id` was a
string in the request body.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import security
from app.main import app

KEYS = {
    "k_agent": security.Principal("k_agent", "Priya Nair", "agent"),
    "k_view": security.Principal("k_view", "Anita Rao", "viewer"),
    "k_admin": security.Principal("k_admin", "Ravi Menon", "admin"),
}


@pytest.fixture
def guarded(monkeypatch):
    monkeypatch.setattr(security, "auth_enabled", lambda: True)
    monkeypatch.setattr(security, "_registry", lambda: KEYS)
    return TestClient(app)


def _post(client, path, key=None, **kw):
    headers = {"X-API-Key": key} if key else {}
    return client.post(path, headers=headers, **kw)


def test_no_credential_is_rejected(guarded):
    r = _post(guarded, "/api/calls/whatever/approve", json={})
    assert r.status_code == 401
    # The response must say which header, or the first integrator loses an hour.
    assert "X-API-Key" in r.json()["detail"]


def test_an_unknown_key_is_rejected(guarded):
    assert _post(guarded, "/api/calls/x/approve", "not-a-key", json={}).status_code == 401


def test_a_viewer_cannot_approve(guarded):
    r = _post(guarded, "/api/calls/x/approve", "k_view", json={})
    assert r.status_code == 403
    assert "viewer" in r.json()["detail"]


def test_an_agent_cannot_run_integration_sync(guarded):
    """Roles are checked by capability, not by rank.

    An agent outranks a viewer but still has no business pushing every stored
    call into Firestore and a shared spreadsheet.
    """
    assert _post(guarded, "/api/integrations/sync", "k_agent").status_code == 403
    assert _post(guarded, "/api/integrations/sync", "k_admin").status_code == 200


def test_an_agent_reaches_the_approval_logic(guarded):
    """404 not 401/403: the request got past auth and failed on a missing call."""
    assert _post(guarded, "/api/calls/no-such-call/approve", "k_agent", json={}).status_code == 404


def test_reads_stay_open_so_a_fresh_clone_still_shows_a_dashboard(guarded):
    assert guarded.get("/api/health").status_code == 200
    assert guarded.get("/api/calls").status_code == 200


def test_identity_reports_capabilities_not_just_a_name(guarded):
    me = guarded.get("/api/me", headers={"X-API-Key": "k_agent"}).json()
    assert me["name"] == "Priya Nair"
    assert me["authenticated"] is True
    assert me["can"] == {
        "read": True,
        "call": True,
        "approve": True,
        "integrate": False,
    }


def test_a_wrong_key_is_compared_without_leaking_its_shape(guarded):
    """Keys go through hmac.compare_digest, so a near-miss is not distinguishable.

    Asserting on timing would be flaky; asserting the outcome is identical for a
    prefix match and for nonsense at least pins that no short-circuit was added.
    """
    a = _post(guarded, "/api/calls/x/approve", "k_agen", json={})   # one char short
    b = _post(guarded, "/api/calls/x/approve", "zzzzzzzz", json={})
    assert a.status_code == b.status_code == 401


def test_identity_answers_anonymous_callers_instead_of_rejecting_them(guarded):
    """The dashboard asks this to decide whether to show a sign-in screen.

    A 401 here is indistinguishable from an unreachable server, and the client
    then cannot tell "sign in" from "something is broken" — which is exactly how
    an unauthenticated user ends up looking at a dashboard.
    """
    r = guarded.get("/api/me")   # no key at all
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is False
    assert body["auth_enabled"] is True     # so the client knows to gate
    assert body["can"]["approve"] is False

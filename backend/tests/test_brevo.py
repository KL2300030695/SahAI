"""
Brevo delivery.

Tested against a real HTTP server rather than a mocked client, for the same
reason the CRM connector is: a mock proves the test author's idea of the API,
a stub proves what Brevo would actually receive.

The properties that matter are not "did it POST". They are that a failure never
becomes a `sent`, that the redirect safety valve cannot be bypassed, and that
the two setup failures name their own fix.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.config import get_settings
from app.integrations import brevo

RECEIVED: list[dict] = []
BEHAVIOUR = {"status": 201, "body": {"messageId": "<abc@brevo>"}}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        RECEIVED.append(
            {"payload": json.loads(self.rfile.read(n) or b"{}"),
             "api_key": self.headers.get("api-key")}
        )
        body = json.dumps(BEHAVIOUR["body"]).encode()
        self.send_response(BEHAVIOUR["status"])
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def stub(monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(brevo, "API_URL", f"http://127.0.0.1:{srv.server_port}/v3/smtp/email")
    RECEIVED.clear()
    BEHAVIOUR.update({"status": 201, "body": {"messageId": "<abc@brevo>"}})
    brevo.reset_for_tests()
    yield srv
    srv.shutdown()


def _configure(monkeypatch, **over):
    cfg = {"brevo_api_key": "xkey", "brevo_sender_email": "noreply@payflex.test",
           "brevo_sender_name": "PayFlex", "brevo_redirect_to": ""}
    cfg.update(over)
    s = get_settings()
    for k, v in cfg.items():
        monkeypatch.setattr(s, k, v, raising=False)
    monkeypatch.setattr(brevo, "get_settings", lambda: s)


def _send(**over):
    args = dict(to_email="arun@example.com", to_name="Arun Menon",
                subject="Your Pay-in-3 details", body="Hi Arun,\n\nHere are the fee details.",
                approved_by="Priya Nair", call_id="call-001")
    args.update(over)
    return brevo.send_email(**args)


# --- the happy path, and what Brevo actually receives ----------------------


def test_a_delivered_message_carries_its_provenance(stub, monkeypatch):
    _configure(monkeypatch)
    res = _send()
    assert res.ok and res.message_id == "<abc@brevo>"
    p = RECEIVED[0]["payload"]
    assert RECEIVED[0]["api_key"] == "xkey"
    assert p["to"][0]["email"] == "arun@example.com"
    # So a message in Brevo's log can be traced to the call and the human who
    # released it, without opening this codebase.
    assert p["headers"]["X-SahAI-Call-Id"] == "call-001"
    assert p["headers"]["X-SahAI-Approved-By"] == "Priya Nair"
    assert "call:call-001" in p["tags"]


def test_the_body_is_sent_as_written_not_wrapped_in_marketing_chrome(stub, monkeypatch):
    _configure(monkeypatch)
    _send(body="Hi Arun,\n\nThe late fee is Rs 199.")
    p = RECEIVED[0]["payload"]
    assert p["textContent"] == "Hi Arun,\n\nThe late fee is Rs 199."
    assert "Rs 199" in p["htmlContent"]
    assert "unsubscribe" not in p["htmlContent"].lower()


def test_html_escapes_the_body(stub, monkeypatch):
    _configure(monkeypatch)
    _send(body="5 < 10 & <script>alert(1)</script>")
    assert "<script>" not in RECEIVED[0]["payload"]["htmlContent"]
    assert "&lt;script&gt;" in RECEIVED[0]["payload"]["htmlContent"]


# --- the safety valve -------------------------------------------------------


def test_redirect_overrides_the_customer_address(stub, monkeypatch):
    """Seeded customers carry invented addresses. Without this a demo either
    bounces into a stranger's inbox or burns a new sender's reputation."""
    _configure(monkeypatch, brevo_redirect_to="me@team.test")
    res = _send(to_email="arun@example.com")
    assert res.redirected is True
    p = RECEIVED[0]["payload"]
    assert p["to"][0]["email"] == "me@team.test"
    # The intended recipient must survive, or a redirected demo is five
    # identical emails with no way to tell them apart.
    assert "[to: arun@example.com]" in p["subject"]


def test_redirect_rescues_a_customer_with_no_address(stub, monkeypatch):
    _configure(monkeypatch, brevo_redirect_to="me@team.test")
    assert _send(to_email="").ok is True


def test_no_address_and_no_redirect_fails_before_the_request(stub, monkeypatch):
    """Named locally, because Brevo returns a generic 400 that reads like a
    key problem and sends the reader to the wrong console page."""
    _configure(monkeypatch)
    res = _send(to_email="")
    assert res.ok is False
    assert not RECEIVED
    assert "BREVO_REDIRECT_TO" in res.detail


# --- failures must not become a `sent` --------------------------------------


def test_an_unverified_sender_names_its_own_fix(stub, monkeypatch):
    _configure(monkeypatch)
    BEHAVIOUR.update({"status": 400,
                      "body": {"message": "sender is not valid"}})
    res = _send()
    assert res.ok is False
    assert "Senders & IP" in res.detail


def test_a_rejected_key_says_which_kind_of_key(stub, monkeypatch):
    _configure(monkeypatch)
    BEHAVIOUR.update({"status": 401, "body": {"message": "Key not found"}})
    res = _send()
    assert res.ok is False
    assert "Transactional" in res.detail


def test_unconfigured_is_a_no_op_not_an_error(monkeypatch):
    _configure(monkeypatch, brevo_api_key="")
    assert brevo.enabled() is False
    res = _send()
    assert res.ok is False and "not configured" in res.detail


def test_an_ip_restriction_is_not_mislabelled_a_sender_problem(stub, monkeypatch):
    """Brevo's IP-block message also contains the word "unrecognised".

    Order matters: the sender rule matches that word too, so without the more
    specific case winning, a network restriction sends the reader to Senders &
    IP to re-verify an address that was never the problem.
    """
    _configure(monkeypatch)
    BEHAVIOUR.update({"status": 401, "body": {
        "message": "We have detected you are using an unrecognised IP address "
                   "45.249.79.46. If you performed this action make sure to add "
                   "the new IP address in this link: ...",
        "code": "unauthorized"}})
    res = _send()
    assert res.ok is False
    assert "45.249.79.46" in res.detail
    assert "authorised_ips" in res.detail
    assert "Senders & IP" not in res.detail


def test_a_failed_send_still_reports_that_it_was_redirected(stub, monkeypatch):
    """Otherwise `recipient: me@team.test, redirected: false` reads as though
    the attempt went to the real customer — misleading in exactly the field
    someone checks after a failure."""
    _configure(monkeypatch, brevo_redirect_to="me@team.test")
    BEHAVIOUR.update({"status": 500, "body": {"message": "boom"}})
    res = _send(to_email="arun@example.com")
    assert res.ok is False
    assert res.recipient == "me@team.test"
    assert res.redirected is True


# --- sending to the actual customer ----------------------------------------


def test_direct_bypasses_the_redirect_for_one_message(stub, monkeypatch):
    """Per-approval, by a named human. "Email this actual person" is not a
    deployment setting, so it is not a config flag."""
    _configure(monkeypatch, brevo_redirect_to="me@team.test")
    res = _send(to_email="arun@example.com", direct=True)
    assert res.ok and res.redirected is False
    p = RECEIVED[0]["payload"]
    assert p["to"][0]["email"] == "arun@example.com"
    # No [to: …] prefix — it is going where the subject says.
    assert not p["subject"].startswith("[to:")


def test_direct_to_a_reserved_address_is_refused_before_the_request(stub, monkeypatch):
    """.invalid can never resolve. Brevo accepts the request and the message
    silently disappears, which looks exactly like a delivered email."""
    _configure(monkeypatch, brevo_redirect_to="me@team.test")
    res = _send(to_email="sneha.kulkarni@example.invalid", direct=True)
    assert res.ok is False
    assert not RECEIVED
    assert "reserved test address" in res.detail


def test_the_redirect_still_applies_when_direct_is_not_asked_for(stub, monkeypatch):
    _configure(monkeypatch, brevo_redirect_to="me@team.test")
    res = _send(to_email="arun@example.com", direct=False)
    assert res.redirected is True
    assert RECEIVED[0]["payload"]["to"][0]["email"] == "me@team.test"


def test_resolve_recipient_matches_what_send_would_do(stub, monkeypatch):
    """The preview the dashboard shows must not drift from the real behaviour."""
    from app.integrations.brevo import resolve_recipient

    _configure(monkeypatch, brevo_redirect_to="me@team.test")
    assert resolve_recipient("arun@example.com", direct=False) == ("me@team.test", True)
    assert resolve_recipient("arun@example.com", direct=True) == ("arun@example.com", False)


def test_an_address_change_is_written_to_the_interaction_history():
    """A silent address change would be the one CRM edit with no trail — and
    it is the edit that decides who receives a message a human signed."""
    from fastapi.testclient import TestClient
    from app.crm.db import session_scope
    from app.crm.models import Customer, Interaction
    from app.main import app

    c = TestClient(app)
    with session_scope() as s:
        cust = s.get(Customer, "CUST-1042")
        original = cust.email if cust else None
    if original is None:
        pytest.skip("seeded customer absent")

    try:
        r = c.patch("/api/customers/CUST-1042/email", json={"email": "new@example.com"})
        assert r.status_code == 200
        with session_scope() as s:
            assert s.get(Customer, "CUST-1042").email == "new@example.com"
            notes = [
                i.note
                for i in s.query(Interaction).filter_by(customer_id="CUST-1042").all()
            ]
        assert any("Email changed from" in n and "new@example.com" in n for n in notes)
    finally:
        with session_scope() as s:
            s.get(Customer, "CUST-1042").email = original

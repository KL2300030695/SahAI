"""
CRM connectors.

The REST adapter is tested against a real HTTP server, not a mock of one. A
mocked connector proves the test author's idea of HTTP; a stub server proves the
adapter sends what a CRM would have to receive.

The failure policies are the interesting part, and they differ on purpose.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.crm.backends import RestCrm, SqliteCrm

RECORD = {
    "name": "Arun Menon",
    "city": "Bengaluru",
    "KYC_Status__c": "in_progress",       # deliberately a remote field name
    "do_not_call": False,
    "past_interactions": ["called 12 Jan"],
}
FIELD_MAP = {"kyc_status": "KYC_Status__c"}
WRITES: list[dict] = []


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep pytest output clean
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/nobody"):
            return self._send(404, {})
        self._send(200, RECORD)

    def do_PATCH(self):
        n = int(self.headers.get("Content-Length", 0))
        WRITES.append({
            "body": json.loads(self.rfile.read(n) or b"{}"),
            "auth": self.headers.get("Authorization"),
        })
        self._send(200, {"ok": True})


@pytest.fixture(scope="module")
def crm():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield RestCrm(f"http://127.0.0.1:{srv.server_port}", token="t0ken",
                  field_map=FIELD_MAP, timeout_s=3)
    srv.shutdown()


def test_a_remote_field_name_is_mapped_on_read(crm):
    """Integrating must not mean editing Python."""
    snap = crm.read_snapshot("CUST-1")
    assert snap is not None
    assert snap.name == "Arun Menon"
    assert snap.kyc_status == "in_progress"   # came back as KYC_Status__c


def test_a_write_is_mapped_and_attributed(crm):
    WRITES.clear()
    ok, msg = crm.apply_patch("CUST-1", {"kyc_status": "completed"}, "Priya Nair")
    assert ok, msg
    assert WRITES[0]["body"]["KYC_Status__c"] == "completed"
    assert WRITES[0]["body"]["approved_by"] == "Priya Nair"
    assert WRITES[0]["auth"] == "Bearer t0ken"


def test_an_unreachable_crm_fails_closed_on_do_not_call():
    """The asymmetry that matters.

    If we cannot confirm someone may be contacted, we must not contact them.
    A read failing open would let an outage undo an opt-out.
    """
    dead = RestCrm("http://127.0.0.1:9", timeout_s=0.4)  # nothing listens on 9
    assert dead.is_do_not_call("CUST-1") is True


def test_an_unreachable_crm_degrades_the_snapshot_rather_than_blocking():
    """Less context is worse assistance, not a safety failure — the call goes on."""
    dead = RestCrm("http://127.0.0.1:9", timeout_s=0.4)
    assert dead.read_snapshot("CUST-1") is None


def test_a_failed_write_is_reported_not_swallowed():
    dead = RestCrm("http://127.0.0.1:9", timeout_s=0.4)
    ok, msg = dead.apply_patch("CUST-1", {"kyc_status": "completed"}, "Priya")
    assert ok is False
    assert "pending" in msg


def test_both_adapters_satisfy_the_same_port(crm):
    for backend in (SqliteCrm(), crm):
        for method in ("read_snapshot", "is_do_not_call", "apply_patch", "describe"):
            assert callable(getattr(backend, method))
        assert backend.describe()["connector"] in {"sqlite", "rest"}

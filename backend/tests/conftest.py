"""
Shared test setup.

The one rule enforced here: **the test suite never touches a real external
service.** Not Groq, not Firestore, not Google Sheets.

This exists because it was violated. When the Firestore mirror was wired into
`POST /approve`, the approval-gate tests -- which had no knowledge of Firestore
at all and no mocking -- began writing real documents into the live project on
every run. The suite stayed green throughout; only a manual read-back of the
collection showed `test-approval-gate` sitting alongside the real calls.

The lesson is that opting out per test does not scale: any future test that
happens to call an endpoint inherits whatever side effects that endpoint has
grown since. So mirrors are off by default for everything, and a test that wants
one enables it deliberately with `monkeypatch`.
"""

from __future__ import annotations

import pytest

from app.integrations import firestore_sync, sheets
from app import security


@pytest.fixture(autouse=True)
def _no_external_writes(monkeypatch):
    """Force every outbound mirror off for the duration of each test.

    Patching `enabled()` rather than the config means it holds regardless of
    what `.env` says on the machine running the suite -- a developer with real
    credentials configured and CI with none must get identical behaviour.
    """
    monkeypatch.setattr(firestore_sync, "enabled", lambda: False, raising=True)
    monkeypatch.setattr(sheets, "enabled", lambda: False, raising=True)
    yield


@pytest.fixture(autouse=True)
def _auth_off_by_default(monkeypatch):
    """Run the suite unauthenticated unless a test says otherwise.

    Same reasoning as the mirrors above: a test must behave identically on a
    laptop with real credentials in .env and on CI with none. Turning auth on
    locally broke six tests that had nothing to do with auth, which is the
    signal that they were reading ambient configuration rather than pinning
    behaviour.

    Tests that *are* about access control turn it back on explicitly — see
    `tests/test_auth.py`.
    """
    monkeypatch.setattr(security, "auth_enabled", lambda: False)
    monkeypatch.setattr(security, "_registry", dict)
    yield

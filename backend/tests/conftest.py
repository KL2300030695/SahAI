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

import os
import tempfile
from pathlib import Path

# Point the suite at its own database before ANY app module is imported --
# app.crm.db builds its engine at import time, so setting this later has no
# effect. Without it the suite shares backend/data/sahai.db with a running
# uvicorn, and the two writers collide: the same run passes 197 tests and then
# fails one at random, which is worse than failing consistently because it
# trains everyone to re-run until green.
_TEST_DB = Path(tempfile.gettempdir()) / "sahai-tests.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB.as_posix()}"

import pytest  # noqa: E402

from app.integrations import brevo, firestore_sync, sheets  # noqa: E402
from app import security  # noqa: E402
from app.crm.db import init_db  # noqa: E402
from app.seed.seed_db import seed as seed_db  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _isolated_database():
    """Build the test database once per session."""
    init_db()
    try:
        seed_db()
    except Exception:
        # Seeding is a convenience; tests that need a customer create their own.
        pass
    yield


@pytest.fixture(autouse=True)
def _no_external_writes(monkeypatch):
    """Force every outbound mirror off for the duration of each test.

    Patching `enabled()` rather than the config means it holds regardless of
    what `.env` says on the machine running the suite -- a developer with real
    credentials configured and CI with none must get identical behaviour.
    """
    monkeypatch.setattr(firestore_sync, "enabled", lambda: False, raising=True)
    monkeypatch.setattr(sheets, "enabled", lambda: False, raising=True)
    # Brevo actually delivers email. It was added after this fixture was written
    # and was not covered by it -- so a machine with BREVO_API_KEY set would have
    # sent real messages to seeded customers on every test run.
    monkeypatch.setattr(brevo, "enabled", lambda: False, raising=True)
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

"""
Firestore mirror.

SQLite stays the system of record; Firestore is where the data lives durably and
where anyone else can read it. The split is deliberate rather than half-hearted:

`get_crm_snapshot` runs on **every turn** of a live call, inside a latency budget
of about a second. A Firestore read from India is a 50-200ms round trip, so
putting it in that path would spend a fifth of the budget on network and make the
demo fail whenever the wifi does. SQLite answers in microseconds, gives real
transactions for the approval write, and lets the 178-test suite run offline.

So every write goes to SQLite first and is mirrored here afterwards. All the data
ends up in Firestore; the customer on the phone just never waits for it.

Everything in this module is **best-effort**. A Firestore outage must never fail
a call that otherwise succeeded -- an agent mid-conversation does not care that a
cloud mirror is unreachable, and losing the turn would be a far worse outcome
than a stale document. Failures are counted and surfaced at
`/api/integrations/status` rather than raised.

Layout (prefix configurable so a shared project does not collide):

    sahai_calls/{call_id}                  one document per call
    sahai_calls/{call_id}/stages/{n}       the pipeline trace, ordered
    sahai_customers/{customer_id}          CRM record as last written
"""

from __future__ import annotations

import threading
from typing import Any, Iterable, Optional

from app.config import get_settings

_client: Any = None
_lock = threading.Lock()
_stats = {"documents": 0, "failures": 0, "last_error": ""}


def enabled() -> bool:
    s = get_settings()
    return bool(s.firestore_enabled) and s.google_ready


def _get_client() -> Any:
    """Lazily build the client. Import is deferred so the dependency stays
    optional -- a clone with no Google setup never imports google.cloud."""
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is not None:
            return _client
        s = get_settings()
        from google.cloud import firestore
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(
            s.google_credentials_path
        )
        _client = firestore.Client(
            project=s.firestore_project or creds.project_id, credentials=creds
        )
        return _client


def _collection(name: str) -> str:
    return f"{get_settings().firestore_prefix}_{name}"


def status() -> dict[str, Any]:
    s = get_settings()
    return {
        "enabled": bool(s.firestore_enabled),
        "credentials_found": s.google_ready,
        "ready": enabled(),
        "project": s.firestore_project or None,
        "prefix": s.firestore_prefix,
        **_stats,
    }


def _record_failure(e: Exception) -> None:
    _stats["failures"] += 1
    _stats["last_error"] = f"{type(e).__name__}: {e}"[:300]


def sync_call(call_row: dict[str, Any], stage_rows: Iterable[dict[str, Any]]) -> bool:
    """Mirror one call and its pipeline trace.

    Takes the same dict shapes the CSV export produces, so the two exports can
    never describe the call differently -- there is one row-building path, and
    both destinations consume it.
    """
    if not enabled():
        return False
    try:
        db = _get_client()
        call_id = str(call_row.get("call_id") or "").strip()
        if not call_id:
            return False

        doc = db.collection(_collection("calls")).document(call_id)
        doc.set(call_row, merge=True)
        written = 1

        # Stages are written in a batch and keyed by ordinal so they read back
        # in pipeline order rather than in whatever order Firestore returns.
        batch = db.batch()
        pending = 0
        for i, stage in enumerate(stage_rows):
            batch.set(doc.collection("stages").document(f"{i:04d}"), stage)
            pending += 1
            # Firestore caps a batch at 500 operations.
            if pending == 450:
                batch.commit()
                written += pending
                batch, pending = db.batch(), 0
        if pending:
            batch.commit()
            written += pending

        _stats["documents"] += written
        return True
    except Exception as e:  # noqa: BLE001 - a mirror must not break a call
        _record_failure(e)
        return False


def sync_customer(customer: dict[str, Any]) -> bool:
    """Mirror a CRM record after an approved write."""
    if not enabled():
        return False
    try:
        cid = str(customer.get("customer_id") or "").strip()
        if not cid:
            return False
        _get_client().collection(_collection("customers")).document(cid).set(
            customer, merge=True
        )
        _stats["documents"] += 1
        return True
    except Exception as e:  # noqa: BLE001
        _record_failure(e)
        return False


def fetch_call(call_id: str) -> Optional[dict[str, Any]]:
    """Read a call back. Used by the status endpoint to prove a round trip."""
    if not enabled():
        return None
    try:
        snap = _get_client().collection(_collection("calls")).document(call_id).get()
        return snap.to_dict() if snap.exists else None
    except Exception as e:  # noqa: BLE001
        _record_failure(e)
        return None


def reset_for_tests() -> None:
    global _client
    _client = None
    _stats.update({"documents": 0, "failures": 0, "last_error": ""})

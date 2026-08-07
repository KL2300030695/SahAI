"""
CRM connectors: one port, several adapters.

The system of record for a real deployment is Salesforce, LeadSquared, Zoho or
an in-house service — not the SQLite table this demo ships with. What matters
architecturally is that nothing outside this module knows which.

The port is deliberately four methods wide, because that is genuinely all the
pipeline needs of a CRM:

    read_snapshot   what the agent should know before speaking
    is_do_not_call  a hard gate, kept separate from the snapshot on purpose
    apply_patch     the only write, and only after a human approves
    describe        what this connector is, for /api/integrations/status

`is_do_not_call` is not folded into `read_snapshot` even though both read the
same record. A snapshot is advisory context for a language model; do-not-call is
a legal obligation checked in code before drafting. Merging them would make the
obligation depend on a field surviving a model's attention, which is exactly the
kind of coupling this file exists to prevent.

Two adapters ship:

* `SqliteCrm`  — the local demo store, transactional, offline, sub-millisecond.
* `RestCrm`    — any HTTP CRM. Configure four URLs and a bearer token; no code.

`RestCrm` is not a mock. It issues real requests against whatever it is pointed
at, and there is a contract test that runs it against a stub server, so what is
being claimed is "this adapter works against an HTTP CRM", not "we imagined one".
"""

from __future__ import annotations

import json
from typing import Any, Optional, Protocol

from sqlalchemy.orm import Session

from app.schemas import CRMSnapshot


class CrmBackend(Protocol):
    """What the pipeline requires of a customer system of record."""

    def read_snapshot(self, customer_id: str) -> Optional[CRMSnapshot]: ...

    def is_do_not_call(self, customer_id: str) -> bool: ...

    def apply_patch(
        self, customer_id: str, patch: dict[str, Any], approved_by: str
    ) -> tuple[bool, str]: ...

    def describe(self) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# SQLite — the shipped default
# ---------------------------------------------------------------------------


class SqliteCrm:
    """The local store. Owns a Session per operation rather than holding one.

    A long-lived session would leak state between calls in a process serving
    several agents; the cost of opening one is nothing next to a network CRM.
    """

    name = "sqlite"

    def _scope(self):
        from app.crm.db import session_scope

        return session_scope()

    def read_snapshot(self, customer_id: str) -> Optional[CRMSnapshot]:
        from app.crm.db import get_crm_snapshot

        with self._scope() as s:
            return get_crm_snapshot(s, customer_id)

    def is_do_not_call(self, customer_id: str) -> bool:
        from app.crm.db import is_do_not_call as _dnc

        with self._scope() as s:
            return _dnc(s, customer_id)

    def apply_patch(
        self, customer_id: str, patch: dict[str, Any], approved_by: str
    ) -> tuple[bool, str]:
        raise NotImplementedError(
            "The SQLite write goes through apply_approved_patch(call_id), which "
            "is transactional with the call row. Kept there rather than "
            "duplicated here."
        )

    def describe(self) -> dict[str, Any]:
        from app.config import get_settings

        return {"connector": self.name, "target": get_settings().database_url}


# ---------------------------------------------------------------------------
# REST — any HTTP CRM
# ---------------------------------------------------------------------------


class RestCrm:
    """A generic HTTP connector, configured rather than coded.

    Field names differ per CRM, so a mapping is configuration too: `kyc_status`
    here may be `KYC_Status__c` there. Without that, "integrating" would mean
    editing Python, which is the thing an integration layer exists to avoid.

    Failure policy differs by method, deliberately:

    * a failed **read** degrades to no snapshot — the call continues with less
      context, which is worse assistance but not a safety problem;
    * a failed **do-not-call** check returns True — if we cannot confirm someone
      may be contacted, we must not contact them;
    * a failed **write** returns False and the caller keeps the local state as
      pending, so nothing is silently lost.
    """

    name = "rest"

    def __init__(
        self,
        base_url: str,
        token: str = "",
        field_map: Optional[dict[str, str]] = None,
        timeout_s: float = 6.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.field_map = field_map or {}
        self.timeout_s = timeout_s

    # -- plumbing ---------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers=self._headers(),
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            raw = r.read()
        return json.loads(raw) if raw else {}

    def _out(self, field: str) -> str:
        """Local field name -> the remote CRM's name for it."""
        return self.field_map.get(field, field)

    def _in(self, payload: dict[str, Any], field: str, default: Any = None) -> Any:
        return payload.get(self._out(field), payload.get(field, default))

    # -- the port ---------------------------------------------------------

    def read_snapshot(self, customer_id: str) -> Optional[CRMSnapshot]:
        try:
            d = self._request("GET", f"/customers/{customer_id}")
        except Exception:
            return None  # less context, not a blocked call
        if not d:
            return None
        return CRMSnapshot(
            customer_id=customer_id,
            name=str(self._in(d, "name", "") or ""),
            city=str(self._in(d, "city", "") or ""),
            kyc_status=str(self._in(d, "kyc_status", "not_started") or "not_started"),
            credit_limit_inr=self._in(d, "credit_limit_inr"),
            past_interactions=[str(x) for x in (self._in(d, "past_interactions") or [])],
            last_disposition=self._in(d, "last_disposition"),
        )

    def is_do_not_call(self, customer_id: str) -> bool:
        try:
            d = self._request("GET", f"/customers/{customer_id}")
        except Exception:
            # Fail closed. An unreachable CRM is not permission to call someone
            # who may have opted out.
            return True
        return bool(self._in(d, "do_not_call", False))

    def apply_patch(
        self, customer_id: str, patch: dict[str, Any], approved_by: str
    ) -> tuple[bool, str]:
        remote = {self._out(k): v for k, v in patch.items()}
        remote["approved_by"] = approved_by
        try:
            self._request("PATCH", f"/customers/{customer_id}", remote)
        except Exception as e:  # noqa: BLE001
            return False, f"CRM write failed ({type(e).__name__}); left pending."
        return True, f"Applied {len(patch)} field(s) via {self.base_url}."

    def describe(self) -> dict[str, Any]:
        return {
            "connector": self.name,
            "target": self.base_url,
            "authenticated": bool(self.token),
            "mapped_fields": sorted(self.field_map),
        }


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

_backend: Optional[CrmBackend] = None


def get_crm_backend() -> CrmBackend:
    global _backend
    if _backend is not None:
        return _backend
    from app.config import get_settings

    s = get_settings()
    if s.crm_backend == "rest" and s.crm_base_url:
        try:
            field_map = json.loads(s.crm_field_map or "{}")
        except json.JSONDecodeError:
            field_map = {}
        _backend = RestCrm(s.crm_base_url, s.crm_token, field_map)
    else:
        _backend = SqliteCrm()
    return _backend


def reset_backend() -> None:
    global _backend
    _backend = None

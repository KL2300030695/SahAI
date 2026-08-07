"""
Authentication, and the identity that signs an approval.

The point of this module is not "add auth because enterprises want auth". It is
that the system's central safety claim was unenforced.

`POST /approve` is the only route that writes an AI-proposed patch onto a
customer record, and its docstring says no agent can call it. That was true only
in the sense that no agent *did*. The approver's name arrived as a string in the
request body, so anyone who could reach the port could approve as anyone --
including an agent process, and including a script typing "Subhash". A human-
oversight gate whose identity is self-asserted is a convention, not a control.

The fix is one line of principle: **the approver is who the credential says they
are, never who the request says they are.** The body no longer carries a name.

Roles
-----
`agent`      run calls, approve their own write-ups
`viewer`     read dashboards, exports and traces; cannot approve or start calls
`admin`      everything, plus the integration sync endpoints

Configuration is `API_KEYS` in .env, one principal per comma-separated entry:

    API_KEYS=k_live_abc:Priya Nair:agent,k_live_xyz:Ravi Menon:admin

With `AUTH_ENABLED=0` the app runs open with a single local principal, which is
what a fresh clone does. That mode is reported at `/api/health` and stamped onto
every approval, so an unauthenticated write is never silently indistinguishable
from an authenticated one in the audit trail.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, Header, HTTPException

from app.config import get_settings

ROLES = ("viewer", "agent", "admin")

#: What each role may do. Checked by name rather than by rank so that adding a
#: role later cannot silently widen an existing one.
_PERMISSIONS: dict[str, set[str]] = {
    "viewer": {"read"},
    "agent": {"read", "call", "approve"},
    "admin": {"read", "call", "approve", "integrate"},
}


@dataclass(frozen=True)
class Principal:
    key_id: str
    name: str
    role: str
    authenticated: bool = True

    def can(self, action: str) -> bool:
        return action in _PERMISSIONS.get(self.role, set())

    @property
    def audit_name(self) -> str:
        """How this identity appears in an audit record.

        An unauthenticated principal is marked, so a reviewer reading the CRM
        history can tell a signed approval from one made while the service was
        running open. Silently writing "local" as if it were a person would make
        the two indistinguishable a year later.
        """
        return self.name if self.authenticated else f"{self.name} (unauthenticated)"


LOCAL = Principal(
    key_id="local", name="local operator", role="admin", authenticated=False
)


def _parse_keys(raw: str) -> dict[str, Principal]:
    out: dict[str, Principal] = {}
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = [p.strip() for p in entry.split(":")]
        if len(parts) < 2:
            continue
        key, name = parts[0], parts[1]
        role = parts[2] if len(parts) > 2 and parts[2] in ROLES else "agent"
        if key:
            out[key] = Principal(key_id=key[:6] + "…", name=name, role=role)
    return out


def _registry() -> dict[str, Principal]:
    return _parse_keys(get_settings().api_keys)


def auth_enabled() -> bool:
    s = get_settings()
    return bool(s.auth_enabled) and bool(_registry())


def principal_for(api_key: Optional[str]) -> Optional[Principal]:
    """Resolve a key, in constant time with respect to the key's value.

    `hmac.compare_digest` rather than dict lookup on the raw key: a plain
    comparison leaks length and prefix information through timing. It costs
    nothing here and removes a category of question from a security review.
    """
    if not auth_enabled():
        return LOCAL
    if not api_key:
        return None
    for candidate, principal in _registry().items():
        if hmac.compare_digest(candidate, api_key):
            return principal
    return None


def get_principal(x_api_key: Optional[str] = Header(None)) -> Principal:
    p = principal_for(x_api_key)
    if p is None:
        raise HTTPException(
            401,
            "Missing or invalid API key. Send it as the X-API-Key header.",
            headers={"WWW-Authenticate": "X-API-Key"},
        )
    return p


def requires(action: str):
    """Dependency factory: allow the request only if the principal may act."""

    def _dep(principal: Principal = Depends(get_principal)) -> Principal:
        if not principal.can(action):
            raise HTTPException(
                403,
                f"Your role ({principal.role}) cannot {action}.",
            )
        return principal

    return _dep


def principal_for_socket(api_key: Optional[str]) -> Optional[Principal]:
    """WebSocket variant.

    Browsers cannot set headers on a WebSocket handshake, so the key arrives as
    a query parameter. Called out rather than hidden: a URL is likelier to be
    logged by a proxy than a header, which is a real difference in exposure and
    the reason the key belongs in a header everywhere else.
    """
    return principal_for(api_key)

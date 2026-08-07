"""Database session handling and the CRM read/write helpers agents use."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import BACKEND_DIR, get_settings
from app.crm.models import Base, Call, CostRow, Customer, Interaction
from app.schemas import CRMSnapshot, DecisionCost

_settings = get_settings()

# Resolve a relative sqlite URL against the backend dir so the app works
# regardless of the cwd uvicorn was started from.
_url = _settings.database_url
if _url.startswith("sqlite:///./"):
    _url = f"sqlite:///{(BACKEND_DIR / _url.removeprefix('sqlite:///./')).as_posix()}"

engine = create_engine(_url, connect_args={"check_same_thread": False}, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    (BACKEND_DIR / "data").mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(engine)
    _add_missing_columns()


def _add_missing_columns() -> None:
    """Add columns introduced after a database was already created.

    `create_all` creates missing *tables* and silently ignores missing *columns*,
    so a dev database that predates a new field keeps working until the first
    query mentions it and then fails with a bare OperationalError. This is a
    hackathon, not a project with Alembic; an idempotent ADD COLUMN is the honest
    amount of migration machinery for one additive change.
    """
    from sqlalchemy import inspect, text

    wanted = {"cost_rows": {"note": "TEXT DEFAULT ''"}}
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, columns in wanted.items():
            if not insp.has_table(table):
                continue
            have = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in columns.items():
                if name not in have:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


@contextmanager
def session_scope() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get_crm_snapshot(s: Session, customer_id: str) -> Optional[CRMSnapshot]:
    c = s.get(Customer, customer_id)
    if not c:
        return None
    notes = [
        i.note
        for i in sorted(c.interactions, key=lambda x: x.at, reverse=True)[:5]
    ]
    return CRMSnapshot(
        customer_id=c.customer_id,
        name=c.name,
        city=c.city,
        kyc_status=c.kyc_status,
        credit_limit_inr=c.credit_limit_inr,
        past_interactions=notes,
        last_disposition=c.last_disposition,
    )


def is_do_not_call(s: Session, customer_id: str) -> bool:
    """Hard compliance check. Consulted before any follow-up is drafted."""
    c = s.get(Customer, customer_id)
    return bool(c and c.do_not_call)


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def record_cost(s: Session, d: DecisionCost) -> None:
    s.add(
        CostRow(
            call_id=d.call_id,
            turn_index=d.turn_index,
            agent=d.agent,
            tier=d.tier.value,
            model=d.model,
            prompt_tokens=d.prompt_tokens,
            completion_tokens=d.completion_tokens,
            usd=d.usd,
            latency_ms=d.latency_ms,
            escalation_trigger=d.escalation_trigger,
            note=d.note,
        )
    )


def apply_approved_patch(
    s: Session, call_id: str, approver_id: str
) -> tuple[bool, str]:
    """Move a call from proposed to applied.

    This is the ONLY path that writes an AI-proposed patch onto the customer
    record, and it requires a named human approver. The CRM agent cannot reach
    this function -- it is called from the approval endpoint alone.
    """
    call = s.get(Call, call_id)
    if not call:
        return False, "call not found"
    if call.send_status != "pending_agent_approval":
        return False, f"call is already {call.send_status}"

    patch = json.loads(call.crm_patch_json or "{}")
    customer = s.get(Customer, call.customer_id)
    if not customer:
        return False, "customer not found"

    writable = {
        "kyc_status",
        "kyc_last_step",
        "city",
        "credit_limit_inr",
        "last_disposition",
        "do_not_call",
    }
    for key, value in patch.items():
        if key in writable:
            setattr(customer, key, value)

    if call.disposition:
        customer.last_disposition = call.disposition
    if call.disposition == "not_interested":
        # Opt-out is honoured regardless of what the patch happened to contain.
        customer.do_not_call = True

    s.add(
        Interaction(
            customer_id=customer.customer_id,
            channel="call",
            note=call.summary or f"Call {call_id} completed.",
        )
    )

    call.send_status = "approved"
    call.approved_by = approver_id
    call.approved_at = datetime.now(timezone.utc)
    return True, "applied"

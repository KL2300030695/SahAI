"""
Mock CRM schema.

Two things here are load-bearing for the guardrails rather than cosmetic:

* `Customer.do_not_call` -- a hard suppression flag. The follow-up path checks it
  in code before anything is drafted or sent. It is not something a model can
  reason its way past.
* `Call.send_status` / `approved_by` -- the human-oversight state machine. The
  CRM agent can only ever write PENDING_AGENT_APPROVAL. Only the approval
  endpoint, with a named human approver, moves it forward.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customers"

    customer_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    phone_masked: Mapped[str] = mapped_column(String(32), default="")
    #: Where an approved follow-up is actually delivered. Unmasked, unlike the
    #: phone: a masked address cannot be sent to, and storing a fake one would
    #: mean "sent" silently meaning nothing.
    email: Mapped[str] = mapped_column(String(200), default="")
    city: Mapped[str] = mapped_column(String(64), default="")
    kyc_status: Mapped[str] = mapped_column(String(32), default="not_started")
    kyc_last_step: Mapped[int] = mapped_column(Integer, default=0)
    credit_limit_inr: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_disposition: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Hard compliance flag. Checked in code before any follow-up is drafted.
    do_not_call: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )

    interactions: Mapped[list["Interaction"]] = relationship(
        back_populates="customer", cascade="all, delete-orphan"
    )
    calls: Mapped[list["Call"]] = relationship(back_populates="customer")


class Interaction(Base):
    __tablename__ = "interactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.customer_id"))
    at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    channel: Mapped[str] = mapped_column(String(24), default="call")
    note: Mapped[str] = mapped_column(Text, default="")

    customer: Mapped[Customer] = relationship(back_populates="interactions")


class Call(Base):
    __tablename__ = "calls"

    call_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.customer_id"))
    agent_name: Mapped[str] = mapped_column(String(80), default="")

    # Code-level gate: the orchestrator refuses to process turns without this.
    consent_ack: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    started_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    disposition: Mapped[str | None] = mapped_column(String(32), nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    dropoff_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Proposed, not applied. Written to the customer row only on approval.
    crm_patch_json: Mapped[str] = mapped_column(Text, default="{}")
    followup_json: Mapped[str] = mapped_column(Text, default="{}")

    # Human-oversight state machine.
    send_status: Mapped[str] = mapped_column(
        String(32), default="pending_agent_approval"
    )
    approved_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    guardrail_trace_json: Mapped[str] = mapped_column(Text, default="[]")
    transcript_json: Mapped[str] = mapped_column(Text, default="[]")
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    customer: Mapped[Customer] = relationship(back_populates="calls")


class CostRow(Base):
    """One agent decision. The ledger is persisted so a demo run can be replayed
    and audited after the fact rather than only printed to stdout."""

    __tablename__ = "cost_rows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    call_id: Mapped[str] = mapped_column(String(64), index=True)
    turn_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    agent: Mapped[str] = mapped_column(String(48))
    tier: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(80), default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    escalation_trigger: Mapped[str | None] = mapped_column(String(160), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

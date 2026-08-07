"""
Agent base class.

An agent owns exactly one decision, declares the tier it runs at, and speaks
only in schema types. Agents never import each other — the orchestrator is the
only thing that knows the pipeline shape. That is what keeps this a set of
cooperating specialists rather than one prompt with headings.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Generic, Optional, TypeVar

from pydantic import BaseModel

from app.llm.client import LLMClient, LLMResponse, get_llm
from app.schemas import ModelTier
from app.telemetry.cost import CostMeter

TIn = TypeVar("TIn", bound=BaseModel)
TOut = TypeVar("TOut", bound=BaseModel)


class Agent(ABC, Generic[TIn, TOut]):
    name: str = "agent"
    tier: ModelTier = ModelTier.CHEAP

    def __init__(self, llm: Optional[LLMClient] = None) -> None:
        self.llm = llm or get_llm()

    @abstractmethod
    def run(
        self,
        inp: TIn,
        *,
        meter: Optional[CostMeter] = None,
        turn_index: Optional[int] = None,
    ) -> TOut:
        """Execute the decision. Implementations record their own cost."""

    # -- helper ----------------------------------------------------------

    def _record(
        self,
        meter: Optional[CostMeter],
        resp: LLMResponse,
        *,
        turn_index: Optional[int] = None,
        escalation_trigger: Optional[str] = None,
    ) -> None:
        if meter is not None:
            meter.record_llm(
                self.name,
                resp,
                turn_index=turn_index,
                escalation_trigger=escalation_trigger,
            )

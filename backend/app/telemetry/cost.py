"""
Per-decision cost ledger.

Every agent decision appends a row: which agent, which tier, which model, the
real token counts from the API response, the priced cost, and -- when the router
escalated -- the named rule that caused it.

The point is that the cost-per-call number in the pitch is *measured*. Nothing
here estimates. Zero-cost steps (retrieval, code guardrails) are recorded too, as
tier NONE with usd=0, because "how much of this pipeline runs without an LLM" is
itself the headline finding.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

from app.config import FRONTIER_BASELINE, get_settings
from app.llm.client import LLMResponse
from app.schemas import CostLedger, DecisionCost, ModelTier


class CostMeter:
    """Accumulates decisions for one call."""

    def __init__(self, call_id: str) -> None:
        self.call_id = call_id
        self.rows: list[DecisionCost] = []

    # -- recording -------------------------------------------------------

    def record_llm(
        self,
        agent: str,
        resp: LLMResponse,
        *,
        turn_index: Optional[int] = None,
        escalation_trigger: Optional[str] = None,
    ) -> DecisionCost:
        row = DecisionCost(
            call_id=self.call_id,
            turn_index=turn_index,
            agent=agent,
            tier=resp.tier,
            model=resp.model,
            prompt_tokens=resp.prompt_tokens,
            completion_tokens=resp.completion_tokens,
            usd=resp.usd,
            latency_ms=resp.latency_ms,
            escalation_trigger=escalation_trigger,
        )
        self.rows.append(row)
        return row

    def record_local(
        self,
        agent: str,
        *,
        latency_ms: float = 0.0,
        turn_index: Optional[int] = None,
        note: str = "",
    ) -> DecisionCost:
        """A pipeline step served by local compute. Costs nothing; still counted."""
        row = DecisionCost(
            call_id=self.call_id,
            turn_index=turn_index,
            agent=agent,
            tier=ModelTier.NONE,
            model="local",
            usd=0.0,
            latency_ms=latency_ms,
            escalation_trigger=note or None,
        )
        self.rows.append(row)
        return row

    def record_stt(
        self, agent: str, model: str, usd: float, latency_ms: float
    ) -> DecisionCost:
        row = DecisionCost(
            call_id=self.call_id,
            agent=agent,
            tier=ModelTier.STT,
            model=model,
            usd=usd,
            latency_ms=latency_ms,
        )
        self.rows.append(row)
        return row

    # -- rollup ----------------------------------------------------------

    @property
    def total_usd(self) -> float:
        return sum(r.usd for r in self.rows)

    def ledger(self) -> CostLedger:
        s = get_settings()
        by_tier: dict[str, float] = defaultdict(float)
        for r in self.rows:
            by_tier[r.tier.value] += r.usd
        total = self.total_usd
        return CostLedger(
            call_id=self.call_id,
            decisions=self.rows,
            total_usd=round(total, 8),
            total_inr=round(total * s.usd_to_inr, 6),
            by_tier_usd={k: round(v, 8) for k, v in sorted(by_tier.items())},
            llm_calls=sum(1 for r in self.rows if r.tier != ModelTier.NONE),
            zero_cost_steps=sum(1 for r in self.rows if r.tier == ModelTier.NONE),
        )

    # -- comparison ------------------------------------------------------

    def frontier_baseline_usd(self) -> float:
        """What this call's token volume would have cost through a single
        frontier-model mega-prompt, for the cost-reduction comparison.

        Deliberately conservative: it prices only the tokens we actually spent.
        A real mega-prompt implementation would also carry the whole KB in
        context on every turn instead of retrieving 4 chunks, so the true gap is
        wider than this figure suggests.
        """
        p = sum(r.prompt_tokens for r in self.rows)
        c = sum(r.completion_tokens for r in self.rows)
        return (
            p * FRONTIER_BASELINE["input_usd_per_mtok"]
            + c * FRONTIER_BASELINE["output_usd_per_mtok"]
        ) / 1_000_000

    def summary_line(self) -> str:
        s = get_settings()
        total = self.total_usd
        baseline = self.frontier_baseline_usd()
        ratio = (baseline / total) if total > 0 else 0.0
        return (
            f"call={self.call_id} "
            f"decisions={len(self.rows)} "
            f"llm_calls={sum(1 for r in self.rows if r.tier != ModelTier.NONE)} "
            f"free_steps={sum(1 for r in self.rows if r.tier == ModelTier.NONE)} "
            f"cost=${total:.6f} (₹{total * s.usd_to_inr:.4f}) "
            f"frontier_equivalent=${baseline:.6f} "
            f"reduction={ratio:.1f}x"
        )


def render_ledger_table(ledger: CostLedger) -> str:
    """Human-readable ledger for the end of a demo run."""
    s = get_settings()
    lines = [
        "",
        f"  COST LEDGER — {ledger.call_id}",
        "  " + "-" * 92,
        f"  {'turn':>4}  {'agent':<20} {'tier':<9} {'model':<30} {'tok in/out':>12} {'usd':>10}",
        "  " + "-" * 92,
    ]
    for r in ledger.decisions:
        turn = "-" if r.turn_index is None else str(r.turn_index)
        toks = f"{r.prompt_tokens}/{r.completion_tokens}"
        lines.append(
            f"  {turn:>4}  {r.agent:<20} {r.tier.value:<9} {r.model[:30]:<30} "
            f"{toks:>12} {r.usd:>10.6f}"
        )
    lines += [
        "  " + "-" * 92,
        f"  LLM calls: {ledger.llm_calls}    zero-cost local steps: {ledger.zero_cost_steps}",
        "  by tier: "
        + ", ".join(f"{k}=${v:.6f}" for k, v in ledger.by_tier_usd.items()),
        f"  TOTAL: ${ledger.total_usd:.6f}  (₹{ledger.total_usd * s.usd_to_inr:.4f})",
        "",
    ]
    return "\n".join(lines)

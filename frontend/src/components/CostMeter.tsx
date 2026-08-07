import type { CostLedger } from "../lib/types";
import { TIER_LABEL, TierChip } from "./Bits";

/**
 * Live cost ledger.
 *
 * Every figure comes from the `usage` block of a real API response, priced
 * against the rate table in config.py. Nothing here is estimated — which is the
 * whole reason it is worth showing.
 */
export default function CostMeter({
  ledger,
  frontierUsd,
}: {
  ledger: CostLedger | null;
  frontierUsd: number;
}) {
  if (!ledger) {
    return (
      <div className="panel">
        <div className="panel-title">Cost ledger</div>
        <div className="px-3 py-6 text-center text-xs text-slate-600">
          Starts recording when the call begins.
        </div>
      </div>
    );
  }

  const reduction = ledger.total_usd > 0 ? frontierUsd / ledger.total_usd : 0;
  const tiers = Object.entries(ledger.by_tier_usd).filter(([, v]) => v > 0 || true);
  const max = Math.max(...tiers.map(([, v]) => v), 1e-9);

  return (
    <div className="panel">
      <div className="panel-title flex items-center justify-between">
        <span>Cost ledger</span>
        <span className="normal-case tracking-normal text-slate-500">measured</span>
      </div>

      <div className="border-b border-slate-800 px-3 py-3">
        <div className="flex items-baseline gap-2">
          <span className="font-mono text-2xl font-semibold text-emerald-400">
            ${ledger.total_usd.toFixed(6)}
          </span>
          <span className="text-xs text-slate-500">
            ₹{ledger.total_inr.toFixed(4)}
          </span>
        </div>
        <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-slate-500">
          <span>{ledger.llm_calls} LLM calls</span>
          <span className="text-emerald-500/80">
            {ledger.zero_cost_steps} steps at $0
          </span>
        </div>
      </div>

      {reduction > 1 && (
        <div className="border-b border-slate-800 bg-emerald-950/20 px-3 py-2">
          <div className="flex items-baseline justify-between">
            <span className="text-[11px] text-slate-400">
              Same tokens on one frontier model
            </span>
            <span className="font-mono text-xs text-slate-400">
              ${frontierUsd.toFixed(4)}
            </span>
          </div>
          <div className="mt-1 text-sm font-semibold text-emerald-400">
            {reduction.toFixed(1)}× cheaper
          </div>
        </div>
      )}

      <div className="space-y-1.5 px-3 py-2.5">
        {tiers.map(([tier, usd]) => (
          <div key={tier} className="flex items-center gap-2">
            <div className="w-24 shrink-0">
              <TierChip tier={tier} />
            </div>
            <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-slate-800">
              <div
                className="h-full rounded-full bg-sky-500/60"
                style={{ width: `${Math.max(2, (usd / max) * 100)}%` }}
              />
            </div>
            <span className="w-20 shrink-0 text-right font-mono text-[10px] text-slate-500">
              ${usd.toFixed(6)}
            </span>
          </div>
        ))}
      </div>

      <details className="border-t border-slate-800">
        <summary className="cursor-pointer px-3 py-2 text-[11px] text-slate-500 hover:text-slate-300">
          Per-decision breakdown ({ledger.decisions.length})
        </summary>
        <div className="max-h-64 overflow-y-auto border-t border-slate-800">
          <table className="w-full text-[10px]">
            <thead className="sticky top-0 bg-slate-900 text-slate-600">
              <tr>
                <th className="px-2 py-1 text-left font-medium">turn</th>
                <th className="px-2 py-1 text-left font-medium">agent</th>
                <th className="px-2 py-1 text-left font-medium">tier</th>
                <th className="px-2 py-1 text-right font-medium">tok</th>
                <th className="px-2 py-1 text-right font-medium">usd</th>
              </tr>
            </thead>
            <tbody className="font-mono">
              {ledger.decisions.map((d, i) => (
                <tr
                  key={i}
                  className="border-t border-slate-800/50"
                  title={d.escalation_trigger ?? TIER_LABEL[d.tier] ?? ""}
                >
                  <td className="px-2 py-1 text-slate-600">
                    {d.turn_index ?? "—"}
                  </td>
                  <td className="px-2 py-1 text-slate-400">{d.agent}</td>
                  <td className="px-2 py-1">
                    <TierChip tier={d.tier} />
                  </td>
                  <td className="px-2 py-1 text-right text-slate-500">
                    {d.prompt_tokens}/{d.completion_tokens}
                  </td>
                  <td className="px-2 py-1 text-right text-slate-400">
                    {d.usd.toFixed(6)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}

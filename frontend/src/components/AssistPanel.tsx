import type { TurnAssist } from "../lib/types";
import { Meter, SentimentChip, TierPath } from "./Bits";
import GuardrailTrace from "./GuardrailTrace";

/** Everything the human agent sees for the current customer turn. */
export default function AssistPanel({ a }: { a: TurnAssist }) {
  return (
    <div className="space-y-3">
      {/* ---- intent ---- */}
      {a.intent && (
        <div className="panel">
          <div className="panel-title">Detected intent</div>
          <div className="space-y-2 px-3 py-2.5">
            <div className="flex flex-wrap items-center gap-2">
              <span className="rounded bg-sky-500/15 px-2 py-0.5 text-xs font-semibold text-sky-300 ring-1 ring-inset ring-sky-500/30">
                {a.intent.intent}
              </span>
              <SentimentChip sentiment={a.intent.sentiment} />
              {Object.entries(a.intent.entities).map(([k, v]) => (
                <span
                  key={k}
                  className="chip bg-slate-800 text-slate-300 ring-slate-700"
                >
                  {k}: {v}
                </span>
              ))}
            </div>
            <Meter label="confidence" value={a.intent.confidence} danger={2} />
            <Meter label="drop-off" value={a.intent.dropoff_risk} />

            {a.intent.buying_signals.length > 0 && (
              <div className="rounded border border-emerald-800/50 bg-emerald-950/25 px-2 py-1.5">
                <div className="mb-0.5 text-[10px] font-semibold uppercase tracking-wide text-emerald-400">
                  Buying signal — move to close
                </div>
                {a.intent.buying_signals.map((s, i) => (
                  <p key={i} className="text-[11px] italic text-emerald-200/80">
                    “{s}”
                  </p>
                ))}
              </div>
            )}

            {(a.intent.sentiment === "angry" ||
              a.intent.sentiment === "frustrated" ||
              a.intent.intent === "complaint" ||
              a.intent.intent === "payment_issue") && (
              <div className="rounded border border-orange-800/50 bg-orange-950/25 px-2 py-1.5">
                <p className="text-[11px] leading-snug text-orange-200/90">
                  <strong>Stop selling.</strong> This caller has a problem to
                  resolve — acknowledge and route them. Don't mention the
                  product, offers, or onboarding.
                </p>
              </div>
            )}

            {a.intent.rationale && (
              <p className="text-[11px] italic leading-snug text-slate-500">
                {a.intent.rationale}
              </p>
            )}
          </div>
        </div>
      )}

      {/* ---- suggestion ---- */}
      {a.nba && !a.blocked && (
        <div className="panel border-emerald-800/50">
          <div className="panel-title flex items-center justify-between border-emerald-900/50">
            <span className="text-emerald-400">Suggested next action</span>
            <span className="chip bg-slate-800 text-slate-400 ring-slate-700">
              {a.nba.action_type}
            </span>
          </div>
          <div className="px-3 py-3">
            <p className="text-sm leading-relaxed text-slate-100">“{a.nba.say}”</p>
            {a.nba.why && (
              <p className="mt-2 border-t border-slate-800 pt-2 text-[11px] leading-snug text-slate-500">
                <span className="text-slate-400">Why:</span> {a.nba.why}
              </p>
            )}
            {a.nba.requires_human_confirmation && (
              <div className="mt-2 flex items-start gap-1.5 rounded border border-amber-800/50 bg-amber-950/30 px-2 py-1.5">
                <span className="text-xs text-amber-400">⚑</span>
                <p className="text-[11px] leading-snug text-amber-200/80">
                  Touches credit terms — <strong>you</strong> confirm and speak
                  this. The assistant cannot finalise terms with a customer.
                </p>
              </div>
            )}
          </div>
        </div>
      )}

      {a.blocked && (
        <div className="panel border-rose-900/60 bg-rose-950/20">
          <div className="panel-title border-rose-900/50 text-rose-400">
            Suggestion withheld
          </div>
          <p className="px-3 py-3 text-xs leading-relaxed text-rose-200/80">
            {a.guardrail?.blocked_reason ??
              "The self-check blocked this output before it reached you."}
          </p>
        </div>
      )}

      {/* ---- knowledge base ---- */}
      {a.retrieval && (
        <div className="panel">
          <div className="panel-title flex items-center justify-between">
            <span>Knowledge base</span>
            <span className="chip tier-none">retrieval · $0</span>
          </div>
          <div className="max-h-56 overflow-y-auto">
            {a.retrieval.dropped_stale.length > 0 && (
              <div className="border-b border-amber-900/40 bg-amber-950/20 px-3 py-1.5">
                <p className="text-[11px] text-amber-300/90">
                  Filtered {a.retrieval.dropped_stale.length} expired chunk
                  {a.retrieval.dropped_stale.length > 1 ? "s" : ""} before the model
                  saw {a.retrieval.dropped_stale.length > 1 ? "them" : "it"}:{" "}
                  <span className="font-mono">
                    {a.retrieval.dropped_stale.join(", ")}
                  </span>
                </p>
              </div>
            )}
            {a.retrieval.citations.map((c) => {
              const used = a.nba?.cited_chunk_ids.includes(c.chunk_id);
              return (
                <div
                  key={c.chunk_id}
                  className={`border-b border-slate-800/60 px-3 py-2 last:border-0 ${
                    used ? "bg-emerald-950/20" : ""
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate text-[11px] font-medium text-slate-300">
                      {c.title}
                    </span>
                    {used && (
                      <span className="chip shrink-0 bg-emerald-500/10 text-emerald-300 ring-emerald-500/30">
                        cited
                      </span>
                    )}
                  </div>
                  <div className="mt-0.5 font-mono text-[10px] text-slate-600">
                    {c.chunk_id} · {c.version}
                  </div>
                  <p className="mt-1 line-clamp-3 text-[11px] leading-snug text-slate-500">
                    {c.text.slice(0, 220)}
                    {c.text.length > 220 ? "…" : ""}
                  </p>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* ---- guardrails ---- */}
      {a.guardrail && <GuardrailTrace guard={a.guardrail} />}

      {/* ---- routing ---- */}
      <div className="panel">
        <div className="panel-title">Models this turn touched</div>
        <div className="space-y-1.5 px-3 py-2.5">
          <TierPath path={a.tier_path} />
          <div className="text-[11px] text-slate-500">
            {a.latency_ms.toFixed(0)}ms · running total{" "}
            <span className="font-mono text-slate-400">
              ${a.cost_usd.toFixed(6)}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

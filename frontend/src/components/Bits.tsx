import type { Tier } from "../lib/types";

export const TIER_LABEL: Record<string, string> = {
  none: "local · $0",
  tiny: "86M guard",
  cheap: "8B",
  standard: "20B",
  high: "120B",
  safety: "20B safeguard",
  stt: "whisper",
};

export function TierChip({ tier }: { tier: string }) {
  return (
    <span className={`chip tier-${tier}`} title={TIER_LABEL[tier] ?? tier}>
      {tier}
    </span>
  );
}

/** The tier path for one turn: which models the turn actually touched. */
export function TierPath({ path }: { path: string[] }) {
  if (!path.length) return null;
  return (
    <div className="flex flex-wrap items-center gap-1">
      {path.map((t, i) => (
        <span key={i} className="flex items-center gap-1">
          {i > 0 && <span className="text-slate-600 text-[10px]">→</span>}
          <TierChip tier={t} />
        </span>
      ))}
    </div>
  );
}

export function Meter({
  value,
  label,
  danger = 0.6,
}: {
  value: number;
  label: string;
  danger?: number;
}) {
  const pct = Math.round(Math.max(0, Math.min(1, value)) * 100);
  const hot = value >= danger;
  return (
    <div className="flex items-center gap-2">
      <span className="text-[11px] text-slate-500 w-16 shrink-0">{label}</span>
      <div className="h-1.5 flex-1 rounded-full bg-slate-800 overflow-hidden">
        <div
          className={`h-full rounded-full transition-all ${
            hot ? "bg-amber-400" : "bg-emerald-400"
          }`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span
        className={`text-[11px] tabular-nums w-8 text-right ${
          hot ? "text-amber-300" : "text-slate-400"
        }`}
      >
        {pct}%
      </span>
    </div>
  );
}

/** Sentiment chip. Negative states are visually loud on purpose — they change
 *  what the agent should do next, so they must not read as decoration. */
export function SentimentChip({ sentiment }: { sentiment: string }) {
  const style: Record<string, string> = {
    interested: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/40",
    happy: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/40",
    neutral: "bg-slate-700/40 text-slate-300 ring-slate-500/40",
    confused: "bg-sky-500/15 text-sky-300 ring-sky-500/40",
    hesitant: "bg-amber-500/15 text-amber-300 ring-amber-500/40",
    busy: "bg-amber-500/15 text-amber-300 ring-amber-500/40",
    frustrated: "bg-orange-500/20 text-orange-300 ring-orange-500/50",
    angry: "bg-rose-500/20 text-rose-300 ring-rose-500/50",
  };
  return (
    <span className={`chip ${style[sentiment] ?? style.neutral}`}>
      {sentiment}
    </span>
  );
}

export function ProbabilityChip({ value }: { value: string }) {
  const style: Record<string, string> = {
    high: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/40",
    medium: "bg-amber-500/15 text-amber-300 ring-amber-500/40",
    low: "bg-slate-700/40 text-slate-400 ring-slate-600",
  };
  return (
    <span className={`chip ${style[value] ?? style.low}`}>{value}</span>
  );
}

export function Spinner() {
  return (
    <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-slate-600 border-t-sky-400" />
  );
}

export function Empty({ children }: { children: React.ReactNode }) {
  return (
    <div className="px-3 py-6 text-center text-xs text-slate-600">{children}</div>
  );
}

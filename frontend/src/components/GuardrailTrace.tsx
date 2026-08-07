import type { CheckOut, CheckResult } from "../lib/types";

/**
 * The self-check panel.
 *
 * The `enforced_by` badge is the point of this component. Five of the seven
 * checks are deterministic Python that an adversarial customer cannot talk
 * their way past; two are model judgement. Showing which is which is more
 * honest — and more persuasive — than a row of green ticks.
 */

const LABEL: Record<string, string> = {
  consent_recorded: "Consent recorded",
  injection_screen: "Injection screen",
  grounding: "Grounding",
  no_autonomous_credit_terms: "No autonomous credit terms",
  pii_redaction: "PII redaction",
  no_stale_terms: "No stale terms",
  goal_alignment: "Business-goal alignment",
};

function Row({ c }: { c: CheckResult }) {
  const isCode = c.enforced_by === "code";
  return (
    <div className="px-3 py-2 border-b border-slate-800/60 last:border-0">
      <div className="flex items-start gap-2">
        <span
          className={`mt-0.5 text-xs font-bold ${
            c.passed ? "text-emerald-400" : "text-rose-400"
          }`}
        >
          {c.passed ? "✓" : "✗"}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-xs font-medium text-slate-200">
              {LABEL[c.name] ?? c.name}
            </span>
            <span
              className={`chip ${
                isCode
                  ? "bg-emerald-500/10 text-emerald-300 ring-emerald-500/30"
                  : "bg-violet-500/10 text-violet-300 ring-violet-500/30"
              }`}
              title={
                isCode
                  ? "Deterministic Python. Cannot be prompt-injected away."
                  : "Model judgement, adjudicated by a policy-tuned model."
              }
            >
              {isCode ? "code" : "llm"}
            </span>
            {!c.passed && c.severity === "block" && (
              <span className="chip bg-rose-500/10 text-rose-300 ring-rose-500/30">
                blocking
              </span>
            )}
          </div>
          <p className="mt-0.5 text-[11px] leading-snug text-slate-500">{c.detail}</p>
        </div>
      </div>
    </div>
  );
}

export default function GuardrailTrace({ guard }: { guard: CheckOut }) {
  const codeChecks = guard.checks.filter((c) => c.enforced_by === "code").length;
  return (
    <div className="panel">
      <div className="panel-title flex items-center justify-between">
        <span>Self-check</span>
        <span className="normal-case tracking-normal text-slate-500">
          {codeChecks}/{guard.checks.length} enforced in code
        </span>
      </div>

      {guard.blocked_reason && (
        <div className="border-b border-rose-900/50 bg-rose-950/40 px-3 py-2">
          <div className="text-[11px] font-semibold text-rose-300">
            Output blocked before it reached the agent
          </div>
          <p className="mt-0.5 text-[11px] leading-snug text-rose-200/70">
            {guard.blocked_reason}
          </p>
        </div>
      )}

      <div>
        {guard.checks.map((c, i) => (
          <Row key={i} c={c} />
        ))}
      </div>
    </div>
  );
}

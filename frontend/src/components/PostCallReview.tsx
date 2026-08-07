import { useState } from "react";
import { api } from "../lib/api";
import type { PostCallResult } from "../lib/types";
import GuardrailTrace from "./GuardrailTrace";

/**
 * Post-call review and the human approval gate.
 *
 * Nothing on this screen has been written anywhere yet. The CRM patch is a
 * proposal and the follow-up is a draft; both sit at `pending_agent_approval`
 * until a named human approves them. The backend enforces that — this component
 * is the interface to a rule that exists in code, not the rule itself.
 */
export default function PostCallReview({
  result,
  onApproved,
}: {
  result: PostCallResult;
  onApproved: (r: any) => void;
}) {
  const { crm, guardrail } = result;
  const [summary, setSummary] = useState(crm.summary);
  const [body, setBody] = useState(crm.followup_draft?.body ?? "");
  const [approver, setApprover] = useState("");
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);

  const suppressed = !crm.followup_draft;
  const optedOut = crm.disposition === "not_interested";

  async function send(decision: "approve" | "reject") {
    if (!approver.trim()) {
      setError("Enter your agent ID — approvals are attributed to a person.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const r = await api.approve(result.call_id, {
        approver_id: approver.trim(),
        decision,
        edited_summary: summary,
        edited_followup_body: crm.followup_draft ? body : undefined,
      });
      setOutcome(r);
      onApproved(r);
    } catch (e: any) {
      setError(String(e.message ?? e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-3">
      <div className="panel">
        <div className="panel-title flex items-center justify-between">
          <span>Post-call · pending your approval</span>
          <span
            className={`chip ${
              optedOut
                ? "bg-rose-500/10 text-rose-300 ring-rose-500/30"
                : crm.disposition === "converted"
                  ? "bg-emerald-500/10 text-emerald-300 ring-emerald-500/30"
                  : "bg-amber-500/10 text-amber-300 ring-amber-500/30"
            }`}
          >
            {crm.disposition}
          </span>
        </div>

        <div className="space-y-3 px-3 py-3">
          <div>
            <label className="mb-1 block text-[11px] font-medium text-slate-400">
              Call summary <span className="text-slate-600">(editable)</span>
            </label>
            <textarea
              value={summary}
              onChange={(e) => setSummary(e.target.value)}
              rows={5}
              className="w-full rounded border border-slate-800 bg-slate-950 px-2 py-1.5 text-xs leading-relaxed text-slate-200 focus:border-sky-700 focus:outline-none"
            />
          </div>

          {crm.dropoff_reason && (
            <div>
              <div className="text-[11px] font-medium text-slate-400">
                Drop-off reason
              </div>
              <p className="mt-0.5 rounded border border-amber-900/40 bg-amber-950/20 px-2 py-1.5 text-xs text-amber-200/80">
                {crm.dropoff_reason}
              </p>
            </div>
          )}

          <div>
            <div className="mb-1 text-[11px] font-medium text-slate-400">
              Proposed CRM changes{" "}
              <span className="text-slate-600">— not written yet</span>
            </div>
            <div className="rounded border border-slate-800 bg-slate-950 px-2 py-1.5">
              {Object.entries(crm.crm_patch).map(([k, v]) => (
                <div key={k} className="flex justify-between gap-2 py-0.5 text-[11px]">
                  <span className="font-mono text-slate-500">{k}</span>
                  <span className="font-mono text-slate-300">{String(v)}</span>
                </div>
              ))}
            </div>
          </div>

          <div>
            <div className="mb-1 text-[11px] font-medium text-slate-400">
              Follow-up
            </div>
            {suppressed ? (
              <div
                className={`rounded border px-2 py-2 text-[11px] ${
                  optedOut
                    ? "border-rose-900/50 bg-rose-950/20 text-rose-200/80"
                    : "border-slate-800 bg-slate-950 text-slate-500"
                }`}
              >
                {optedOut ? (
                  <>
                    <strong>Suppressed in code.</strong> The customer opted out, so
                    no follow-up is drafted and none can be sent. This is a TRAI
                    compliance rule enforced before drafting — not a model decision.
                  </>
                ) : (
                  <>No follow-up needed for this disposition.</>
                )}
              </div>
            ) : (
              <>
                <div className="mb-1 flex items-center gap-2 text-[11px] text-slate-500">
                  <span className="chip bg-slate-800 text-slate-300 ring-slate-700">
                    {crm.followup_draft!.channel}
                  </span>
                  {crm.followup_draft!.subject && (
                    <span className="truncate">{crm.followup_draft!.subject}</span>
                  )}
                </div>
                <textarea
                  value={body}
                  onChange={(e) => setBody(e.target.value)}
                  rows={5}
                  className="w-full rounded border border-slate-800 bg-slate-950 px-2 py-1.5 text-xs leading-relaxed text-slate-200 focus:border-sky-700 focus:outline-none"
                />
              </>
            )}
          </div>
        </div>
      </div>

      <GuardrailTrace guard={guardrail} />

      {/* ---- the gate ---- */}
      <div className="panel border-sky-900/50">
        <div className="panel-title border-sky-900/50 text-sky-400">
          Human approval required
        </div>
        <div className="space-y-2 px-3 py-3">
          {outcome ? (
            <div className="rounded border border-emerald-800/50 bg-emerald-950/30 px-2 py-2">
              <div className="text-xs font-semibold text-emerald-300">
                {outcome.send_status === "rejected"
                  ? "Rejected — nothing written."
                  : "Approved and applied."}
              </div>
              <p className="mt-0.5 text-[11px] text-emerald-200/70">
                status: <span className="font-mono">{outcome.send_status}</span>
                {outcome.approved_by && <> · by {outcome.approved_by}</>}
              </p>
            </div>
          ) : (
            <>
              <p className="text-[11px] leading-snug text-slate-500">
                Nothing above has been written to the CRM or sent. The status is{" "}
                <span className="font-mono text-slate-400">{crm.send_status}</span>{" "}
                and only a named human can move it forward — no agent in this
                system can reach the endpoint that does.
              </p>
              <input
                value={approver}
                onChange={(e) => setApprover(e.target.value)}
                placeholder="Your agent ID"
                className="w-full rounded border border-slate-800 bg-slate-950 px-2 py-1.5 text-xs text-slate-200 focus:border-sky-700 focus:outline-none"
              />
              {error && <p className="text-[11px] text-rose-400">{error}</p>}
              <div className="flex gap-2">
                <button
                  onClick={() => send("approve")}
                  disabled={busy}
                  className="flex-1 rounded bg-emerald-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-emerald-500 disabled:opacity-40"
                >
                  {busy ? "Applying…" : "Approve & apply"}
                </button>
                <button
                  onClick={() => send("reject")}
                  disabled={busy}
                  className="rounded border border-slate-700 px-3 py-1.5 text-xs font-medium text-slate-300 hover:bg-slate-800 disabled:opacity-40"
                >
                  Reject
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

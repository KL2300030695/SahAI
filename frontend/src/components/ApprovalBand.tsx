import { useState } from "react";
import { api } from "../lib/api";
import type { PostCallResult } from "../lib/types";

/**
 * The approval moment — the second of the product's two human moments, and the
 * mirror of consent.
 *
 * The call opened with words a human spoke. It closes with a decision a human
 * signs. Both get the full width and the serif; everything between them is
 * evidence. That symmetry is the human-in-the-loop guardrail expressed as
 * layout rather than as a badge on a card.
 *
 * The CRM change renders as an actual before → after so the agent can see what
 * they are authorising, and their name is required, because an approval nobody
 * is named on is not an approval.
 */
export default function ApprovalBand({
  result,
  customerBefore,
  onApproved,
}: {
  result: PostCallResult;
  customerBefore: Record<string, unknown> | null;
  onApproved: (r: unknown) => void;
}) {
  const { crm } = result;
  const [summary, setSummary] = useState(crm.summary);
  const [body, setBody] = useState(crm.followup_draft?.body ?? "");
  const [approver, setApprover] = useState("");
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);

  const optedOut = crm.disposition === "not_interested";
  const patch = Object.entries(crm.crm_patch);

  async function send(decision: "approve" | "reject") {
    if (!approver.trim()) {
      setError("Your name — an approval nobody is named on isn't an approval.");
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

  if (outcome) {
    return (
      <section
        className="card settle px-7 py-6"
        style={{
          borderLeft: `3px solid ${
            outcome.send_status === "rejected" ? "var(--halt)" : "var(--verified)"
          }`,
        }}
      >
        <p className="t-speech-sm">
          {outcome.send_status === "rejected"
            ? "Rejected. Nothing was written."
            : "Signed off and written to the record."}
        </p>
        <p className="mt-2 text-[12.5px]" style={{ color: "var(--graphite)" }}>
          {outcome.approved_by && <>by {outcome.approved_by} · </>}
          status <span className="t-data">{outcome.send_status}</span>
        </p>
      </section>
    );
  }

  return (
    <div className="space-y-4">
      {/* ---- the decision, at full weight ---- */}
      <section
        className="card px-7 py-6"
        style={{ borderLeft: "3px solid var(--yourcall)" }}
      >
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <span className="t-label">Your decision</span>
          <span className="tag tag-yourcall">Nothing has been written yet</span>
        </div>

        <p className="t-speech-sm max-w-3xl">
          You're about to write this to {String(customerBefore?.name ?? "the customer")}'s
          record{crm.followup_draft ? " and send them a message" : ""}.
        </p>
        <p
          className="mt-2 max-w-2xl text-[12.5px] leading-relaxed"
          style={{ color: "var(--graphite)" }}
        >
          The co-pilot can't do this itself — it can only propose. You're the
          only thing that can write it.
        </p>

        <div className="mt-5 flex flex-wrap items-end gap-3">
          <div>
            <label className="t-label mb-1.5 block" htmlFor="approver">
              Your name
            </label>
            <input
              id="approver"
              value={approver}
              onChange={(e) => setApprover(e.target.value)}
              placeholder="e.g. priya.n"
              className="card w-52 px-3 py-2 text-[13px]"
            />
          </div>
          <button
            onClick={() => send("approve")}
            disabled={busy}
            className="btn btn-primary"
          >
            {busy ? "Writing…" : "Approve and write"}
          </button>
          <button
            onClick={() => send("reject")}
            disabled={busy}
            className="btn btn-quiet"
          >
            Discard
          </button>
        </div>
        {error && (
          <p className="mt-3 text-[12.5px]" style={{ color: "var(--halt)" }}>
            {error}
          </p>
        )}
      </section>

      {/* ---- what you're signing ---- */}
      <div className="grid gap-4 lg:grid-cols-2">
        <section className="card overflow-hidden">
          <header
            className="flex items-center justify-between border-b px-4 py-2.5"
            style={{ borderColor: "var(--hairline)" }}
          >
            <span className="t-label">How it went</span>
            <div className="flex gap-1.5">
              <span className="tag">{crm.disposition.replace(/_/g, " ")}</span>
              <span
                className={`tag ${
                  crm.conversion_probability === "high" ? "tag-verified" : ""
                }`}
              >
                {crm.conversion_probability} chance
              </span>
            </div>
          </header>

          <div className="space-y-4 p-4">
            {crm.conversion_rationale && (
              <p className="text-[12.5px] leading-relaxed" style={{ color: "var(--graphite)" }}>
                {crm.conversion_rationale}
              </p>
            )}

            <div>
              <label className="t-label mb-1.5 block" htmlFor="summary">
                Note for whoever picks this up
              </label>
              <textarea
                id="summary"
                value={summary}
                onChange={(e) => setSummary(e.target.value)}
                rows={6}
                className="card w-full px-3 py-2 text-[13px] leading-relaxed"
              />
            </div>

            {crm.dropoff_reason && (
              <div>
                <span className="t-label">Why they didn't go ahead</span>
                <p className="mt-1 text-[13px]">{crm.dropoff_reason}</p>
              </div>
            )}

            {(crm.questions_asked.length > 0 || crm.objections.length > 0) && (
              <div className="grid gap-4 sm:grid-cols-2">
                {crm.questions_asked.length > 0 && (
                  <div>
                    <span className="t-label">They asked</span>
                    <ul className="mt-1 space-y-1">
                      {crm.questions_asked.map((q, i) => (
                        <li key={i} className="text-[12.5px] leading-snug">
                          {q}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
                {crm.objections.length > 0 && (
                  <div>
                    <span className="t-label" style={{ color: "var(--yourcall)" }}>
                      Still unconvinced about
                    </span>
                    <ul className="mt-1 space-y-1">
                      {crm.objections.map((o, i) => (
                        <li
                          key={i}
                          className="text-[12.5px] leading-snug"
                          style={{ color: "var(--yourcall)" }}
                        >
                          {o}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            )}
          </div>
        </section>

        <div className="space-y-4">
          {/* before -> after, not a JSON blob */}
          <section className="card overflow-hidden">
            <header className="border-b px-4 py-2.5" style={{ borderColor: "var(--hairline)" }}>
              <span className="t-label">Record changes</span>
            </header>
            <div className="p-4">
              {patch.length === 0 ? (
                <p className="text-[12.5px]" style={{ color: "var(--graphite)" }}>
                  No changes proposed.
                </p>
              ) : (
                <table className="w-full">
                  <tbody>
                    {patch.map(([k, v]) => {
                      const before = customerBefore?.[k];
                      const changed = String(before) !== String(v);
                      return (
                        <tr key={k}>
                          <td className="py-1 pr-3 align-top">
                            <span className="t-data" style={{ color: "var(--graphite)" }}>
                              {k}
                            </span>
                          </td>
                          <td className="py-1 pr-2 align-top">
                            <span
                              className="t-data"
                              style={{
                                color: "var(--graphite)",
                                textDecoration: changed ? "line-through" : "none",
                              }}
                            >
                              {before === undefined || before === null
                                ? "—"
                                : String(before)}
                            </span>
                          </td>
                          <td className="py-1 align-top">
                            <span
                              className="t-data"
                              style={{
                                color: changed ? "var(--yourcall)" : "var(--graphite)",
                              }}
                            >
                              → {String(v)}
                            </span>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
            </div>
          </section>

          {/* follow-up */}
          <section className="card overflow-hidden">
            <header
              className="flex items-center justify-between border-b px-4 py-2.5"
              style={{ borderColor: "var(--hairline)" }}
            >
              <span className="t-label">Message to send</span>
              {crm.followup_timing !== "none" && (
                <span className="tag">{crm.followup_timing.replace(/_/g, " ")}</span>
              )}
            </header>
            <div className="p-4">
              {crm.followup_draft ? (
                <>
                  <div className="mb-2 flex items-center gap-2">
                    <span className="tag">{crm.followup_draft.channel}</span>
                    {crm.followup_draft.subject && (
                      <span className="truncate text-[12.5px]">
                        {crm.followup_draft.subject}
                      </span>
                    )}
                  </div>
                  <textarea
                    value={body}
                    onChange={(e) => setBody(e.target.value)}
                    rows={6}
                    className="card w-full px-3 py-2 text-[13px] leading-relaxed"
                  />
                </>
              ) : optedOut ? (
                <div
                  className="rounded-md px-3 py-2.5"
                  style={{ background: "var(--halt-wash)" }}
                >
                  <p className="text-[12.5px] leading-relaxed" style={{ color: "var(--halt)" }}>
                    <strong>Nothing will be sent.</strong> They asked not to be
                    contacted, so no message was written and none can be. That's
                    enforced in code before drafting — it isn't the model's call.
                  </p>
                </div>
              ) : (
                <p className="text-[12.5px]" style={{ color: "var(--graphite)" }}>
                  No follow-up needed for this outcome.
                </p>
              )}
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}

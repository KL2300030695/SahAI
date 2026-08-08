import { useEffect, useState } from "react";
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
 * they are authorising, signed under the identity their credential carries —
 * because an approval nobody is named on is not an approval, and a name the
 * approver types is not an identity.
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
  /**
   * Who this will be signed as.
   *
   * The band used to ask the agent to type a name, which the server then wrote
   * to the customer record verbatim — so the identity on an audit row was
   * whatever was typed. The server now takes it from the credential and ignores
   * anything sent. Showing the real identity instead of asking for one is the
   * honest version: the deliberate act is the click, not the typing.
   */
  const [me, setMe] = useState<{ name: string; authenticated: boolean } | null>(null);
  useEffect(() => {
    api.me().then(setMe).catch(() => setMe(null));
  }, []);

  /**
   * Where this message would actually land.
   *
   * Shown before the click, not reported after it. A status field explaining
   * that an email went to the wrong person is not a remedy — by then it has
   * arrived. `BREVO_REDIRECT_TO` keeps demo mail out of a stranger's inbox, and
   * overriding it is a per-approval decision by a named human rather than a
   * deployment setting.
   */
  const [dest, setDest] = useState<{
    configured: boolean;
    customer_email: string;
    redirect_active: boolean;
    goes_to_by_default: string;
    direct_possible: boolean;
    direct_blocked_reason: string;
  } | null>(null);
  const [sendDirect, setSendDirect] = useState(false);
  const refreshDest = () =>
    api.deliveryPreview(result.call_id).then(setDest).catch(() => setDest(null));
  useEffect(() => {
    refreshDest();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [result.call_id]);

  /** Editing the address the follow-up goes to, without leaving the decision. */
  const [editingEmail, setEditingEmail] = useState(false);
  const [emailDraft, setEmailDraft] = useState("");
  const [emailBusy, setEmailBusy] = useState(false);
  const customerId = String(customerBefore?.customer_id ?? "");

  async function saveEmail() {
    if (!customerId) return;
    setEmailBusy(true);
    setError(null);
    try {
      await api.setCustomerEmail(customerId, emailDraft.trim());
      await refreshDest();
      setEditingEmail(false);
    } catch (e: any) {
      setError(String(e.message ?? e));
    } finally {
      setEmailBusy(false);
    }
  }
  const [busy, setBusy] = useState(false);
  const [outcome, setOutcome] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);

  const optedOut = crm.disposition === "not_interested";
  const patch = Object.entries(crm.crm_patch);

  /**
   * A failed post-call check belongs *in* the decision, not under it.
   *
   * This band once rendered "Approve and write" unconditionally while the
   * verdict that rejected the message sat in a collapsed strip at the bottom of
   * the page. A draft claiming Pay-in-3 was entirely free went out marked
   * `sent`, with the check that caught it visible on the same screen. The
   * server refuses that now; this is so the agent finds out before clicking
   * rather than from a 400.
   */
  const failed = (result.guardrail?.checks ?? []).filter((c) => !c.passed);
  const hasDraft = !!crm.followup_draft?.body;
  const rewritten = body.trim() !== (crm.followup_draft?.body ?? "").trim();
  const blocked = failed.length > 0 && hasDraft && !rewritten;

  async function send(decision: "approve" | "reject") {
    setBusy(true);
    setError(null);
    try {
      const r = await api.approve(result.call_id, {
        decision,
        send_to_customer: sendDirect,
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
        style={{
          borderLeft: `3px solid ${blocked ? "var(--halt)" : "var(--yourcall)"}`,
        }}
      >
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <span className="t-label">Your decision</span>
          <span className={`tag ${blocked ? "tag-halt" : "tag-yourcall"}`}>
            {blocked ? "This message can't go out as written" : "Nothing has been written yet"}
          </span>
        </div>

        {blocked ? (
          <>
            <p className="t-speech-sm max-w-3xl" style={{ color: "var(--halt)" }}>
              I can't let this message go out.
            </p>
            <ul className="mt-3 max-w-2xl space-y-2">
              {failed.map((c, i) => (
                <li key={i} className="text-[13px] leading-relaxed">
                  <span className="t-data" style={{ color: "var(--halt)" }}>
                    {c.name.replace(/_/g, " ")}
                  </span>
                  <span className="ml-2 tag">{c.enforced_by}</span>
                  <span className="mt-1 block" style={{ color: "var(--graphite)" }}>
                    {c.detail}
                  </span>
                </li>
              ))}
            </ul>
            <p
              className="mt-3 max-w-2xl text-[12.5px] leading-relaxed"
              style={{ color: "var(--ink)" }}
            >
              Rewrite the message below and it becomes yours to send — your name
              goes on the wording, and the change is recorded. The record note
              and CRM changes are unaffected; you can also discard the whole
              thing.
            </p>
          </>
        ) : (
          <>
            <p className="t-speech-sm max-w-3xl">
              You're about to write this to{" "}
              {String(customerBefore?.name ?? "the customer")}'s record
              {crm.followup_draft ? " and send them a message" : ""}.
            </p>
            <p
              className="mt-2 max-w-2xl text-[12.5px] leading-relaxed"
              style={{ color: "var(--graphite)" }}
            >
              The co-pilot can't do this itself — it can only propose. You're the
              only thing that can write it.
            </p>
          </>
        )}

        <div className="mt-5 flex flex-wrap items-end gap-3">
          <div>
            <span className="t-label mb-1.5 block">Signing as</span>
            <p
              className="card px-3 py-2 text-[13px]"
              style={{
                color: me?.authenticated ? "var(--ink)" : "var(--yourcall)",
                borderColor: me?.authenticated
                  ? "var(--hairline)"
                  : "var(--yourcall)",
              }}
              title={
                me?.authenticated
                  ? "Taken from your credential — it cannot be typed"
                  : "This service is running without authentication, so the record will say so"
              }
            >
              {me ? me.name : "…"}
              {me && !me.authenticated && (
                <span className="block text-[11px]">recorded as unauthenticated</span>
              )}
            </p>
          </div>
          <button
            onClick={() => send("approve")}
            disabled={busy || blocked}
            className="btn btn-primary"
            title={
              blocked
                ? "Rewrite the flagged message first — the server refuses this too"
                : undefined
            }
          >
            {busy
              ? "Writing…"
              : blocked
                ? "Rewrite the message first"
                : "Approve and write"}
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
              {blocked ? (
                <span className="tag tag-halt">needs a rewrite</span>
              ) : rewritten && failed.length > 0 ? (
                <span className="tag tag-verified">your wording</span>
              ) : (
                crm.followup_timing !== "none" && (
                  <span className="tag">{crm.followup_timing.replace(/_/g, " ")}</span>
                )
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

                  {/* Where it goes — stated before the click, not after. */}
                  {dest && (
                    <div
                      className="mt-3 rounded-md px-3 py-2.5"
                      style={{
                        background: sendDirect ? "var(--halt-wash)" : "var(--paper)",
                        border: `1px solid ${sendDirect ? "var(--halt)" : "var(--hairline)"}`,
                      }}
                    >
                      {!dest.configured ? (
                        <p className="text-[12px]" style={{ color: "var(--graphite)" }}>
                          No email provider configured — this will be approved and
                          queued, not sent.
                        </p>
                      ) : (
                        <>
                          <div className="flex items-baseline justify-between gap-3">
                            <span className="t-label">Delivers to</span>
                            <span
                              className="t-data text-right"
                              style={{ color: sendDirect ? "var(--halt)" : "var(--ink)" }}
                            >
                              {sendDirect
                                ? dest.customer_email || "—"
                                : dest.goes_to_by_default || "—"}
                            </span>
                          </div>

                          {/* The address itself is editable here, because
                              needing a shell command to make a customer
                              reachable is not a workflow. */}
                          {editingEmail ? (
                            <div className="mt-2 flex flex-wrap items-center gap-2">
                              <input
                                autoFocus
                                type="email"
                                value={emailDraft}
                                placeholder="name@example.com"
                                onChange={(e) => setEmailDraft(e.target.value)}
                                onKeyDown={(e) => {
                                  if (e.key === "Enter") saveEmail();
                                  if (e.key === "Escape") setEditingEmail(false);
                                }}
                                className="card t-data flex-1 px-2.5 py-1.5"
                                style={{ minWidth: 220 }}
                              />
                              <button
                                onClick={saveEmail}
                                disabled={emailBusy}
                                className="btn btn-primary px-3 py-1.5 text-[12px]"
                              >
                                {emailBusy ? "Saving…" : "Save"}
                              </button>
                              <button
                                onClick={() => setEditingEmail(false)}
                                className="text-[12px] underline"
                                style={{ color: "var(--graphite)" }}
                              >
                                cancel
                              </button>
                            </div>
                          ) : (
                            <button
                              onClick={() => {
                                setEmailDraft(dest.customer_email);
                                setEditingEmail(true);
                              }}
                              className="mt-1 text-[11.5px] underline"
                              style={{ color: "var(--graphite)" }}
                            >
                              {dest.customer_email
                                ? `change their address (${dest.customer_email})`
                                : "add an address for this customer"}
                            </button>
                          )}

                          {dest.redirect_active && (
                            <label
                              className="mt-2 flex cursor-pointer items-start gap-2.5"
                              title={
                                dest.direct_possible
                                  ? "Bypass the redirect for this one message"
                                  : dest.direct_blocked_reason
                              }
                            >
                              <input
                                type="checkbox"
                                className="mt-0.5"
                                checked={sendDirect}
                                disabled={!dest.direct_possible}
                                onChange={(e) => setSendDirect(e.target.checked)}
                              />
                              <span className="text-[12px] leading-snug">
                                Send to the customer directly
                                <span
                                  className="block text-[11.5px]"
                                  style={{ color: "var(--graphite)" }}
                                >
                                  {dest.direct_possible
                                    ? `Bypasses the safety redirect for this message only. It will reach ${dest.customer_email}.`
                                    : dest.direct_blocked_reason}
                                </span>
                              </span>
                            </label>
                          )}

                          {sendDirect && (
                            <p
                              className="mt-2 text-[11.5px] leading-snug"
                              style={{ color: "var(--halt)" }}
                            >
                              This reaches a real inbox. Read the message once more
                              before you sign it.
                            </p>
                          )}
                        </>
                      )}
                    </div>
                  )}
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

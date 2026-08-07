import { useEffect, useState } from "react";
import { api } from "../lib/api";

/**
 * The customer record — and specifically, the part of it the co-pilot read.
 *
 * The CRM was invisible during a call. It appeared as four words in the on-air
 * strip and then as a before → after diff once the call was over, which is the
 * wrong end: by then the agent has already said everything they were going to
 * say. Meanwhile the suggestion engine was being handed `kyc_status`,
 * `last_disposition` and the three most recent interaction notes on every turn.
 *
 * That gap mattered for trust more than for convenience. The Say Line underlines
 * every figure it drew from the handbook, so an agent can check it — but the
 * other half of the context shaping that sentence was unreadable. "Why did it
 * suggest that?" had no answer for CRM-driven advice.
 *
 * So the fields marked *read by the co-pilot* are exactly the ones in
 * `nba._format_crm`. Not "roughly the CRM": the same four plus the same three
 * notes, in the same order. If that function changes and this does not, the
 * panel becomes a lie, which is why the correspondence is stated here rather
 * than left to be noticed.
 */

interface Record_ {
  customer_id: string;
  name: string;
  phone_masked: string;
  city: string;
  kyc_status: string;
  kyc_last_step: number | null;
  credit_limit_inr: number | null;
  last_disposition: string | null;
  do_not_call: boolean;
  interactions: { at: string; note: string }[];
}

/** Mirrors `_format_crm`: these reach the model, the rest do not. */
const READ_BY_COPILOT = new Set([
  "name",
  "city",
  "kyc_status",
  "last_disposition",
]);

function Field({
  label,
  value,
  read,
  warn,
}: {
  label: string;
  value: string;
  read?: boolean;
  warn?: boolean;
}) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1">
      <span className="t-label" style={{ textTransform: "none" }}>
        {label}
        {read && (
          <span
            aria-label="read by the co-pilot"
            title="This field is in the prompt the suggestion engine sees"
            style={{ color: "var(--verified)", marginLeft: 5 }}
          >
            ·
          </span>
        )}
      </span>
      <span
        className="t-data text-right"
        style={{ color: warn ? "var(--halt)" : "var(--ink)" }}
      >
        {value}
      </span>
    </div>
  );
}

export default function CustomerRecord({
  customerId,
}: {
  customerId: string | null;
}) {
  const [rec, setRec] = useState<Record_ | null>(null);
  const [missing, setMissing] = useState(false);

  useEffect(() => {
    if (!customerId) return;
    let cancelled = false;
    api
      .customer(customerId)
      .then((r) => !cancelled && setRec(r as unknown as Record_))
      .catch(() => !cancelled && setMissing(true));
    return () => {
      cancelled = true;
    };
  }, [customerId]);

  if (!customerId || missing) return null;

  return (
    <section className="card overflow-hidden">
      <header
        className="flex items-center justify-between border-b px-4 py-2.5"
        style={{ borderColor: "var(--hairline)" }}
      >
        <span className="t-label">Their record</span>
        <span className="tag" title="Fields marked · are in the co-pilot's prompt">
          <span style={{ color: "var(--verified)" }}>·</span> read by the co-pilot
        </span>
      </header>

      {!rec ? (
        <p className="px-4 py-3 text-[12.5px]" style={{ color: "var(--graphite)" }}>
          Loading…
        </p>
      ) : (
        <div className="px-4 py-3">
          {/* Do-not-call is first and loud. It is a legal obligation checked in
              code before any draft is written, not advice — so it should not be
              something the agent has to scan a list to find. */}
          {rec.do_not_call && (
            <p
              className="mb-3 rounded-md px-3 py-2 text-[12.5px] leading-relaxed"
              style={{ background: "var(--halt-wash)", color: "var(--halt)" }}
            >
              <strong>Do not call.</strong> They have opted out. No follow-up can
              be drafted for this record — that is refused in code, before the
              model is asked.
            </p>
          )}

          <Field label="Name" value={rec.name} read={READ_BY_COPILOT.has("name")} />
          <Field label="City" value={rec.city || "—"} read={READ_BY_COPILOT.has("city")} />
          <Field label="Phone" value={rec.phone_masked || "—"} />
          <Field
            label="KYC"
            value={
              rec.kyc_status.replace(/_/g, " ") +
              (rec.kyc_last_step ? ` · step ${rec.kyc_last_step}` : "")
            }
            read={READ_BY_COPILOT.has("kyc_status")}
          />
          <Field
            label="Last outcome"
            value={(rec.last_disposition || "—").replace(/_/g, " ")}
            read={READ_BY_COPILOT.has("last_disposition")}
          />
          {/* Shown, but deliberately not sent to the model. Rule 2 forbids the
              co-pilot predicting or hinting at a limit; putting an existing one
              in its prompt would be handing it the number it must not say. */}
          <Field
            label="Credit limit"
            value={
              rec.credit_limit_inr ? `₹${rec.credit_limit_inr.toLocaleString("en-IN")}` : "not set"
            }
          />

          {rec.interactions.length > 0 && (
            <div className="mt-3 border-t pt-3" style={{ borderColor: "var(--hairline)" }}>
              <span className="t-label">
                History
                <span style={{ color: "var(--verified)", marginLeft: 5 }}>·</span>
                <span className="ml-1 normal-case" style={{ fontWeight: 400 }}>
                  most recent three
                </span>
              </span>
              <ul className="mt-1.5 space-y-1.5">
                {rec.interactions.slice(0, 3).map((i, n) => (
                  <li key={n} className="text-[12.5px] leading-snug">
                    {i.note}
                    <span className="t-data ml-1" style={{ color: "var(--graphite)" }}>
                      {i.at.slice(0, 10)}
                    </span>
                  </li>
                ))}
              </ul>
              {rec.interactions.length > 3 && (
                <p className="mt-1.5 text-[11.5px]" style={{ color: "var(--graphite)" }}>
                  {rec.interactions.length - 3} older note
                  {rec.interactions.length - 3 > 1 ? "s" : ""} not sent to the model
                </p>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

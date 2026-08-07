import { useState } from "react";

/**
 * The consent moment — the first of the product's two human moments.
 *
 * A full-viewport screen rather than a modal over a dimmed dashboard, because
 * this is not an interruption to the work; it *is* the work, for ten seconds.
 *
 * The script is set in the same serif at the same size as the Say Line, because
 * the agent is about to read it aloud. That is the point of the serif/sans
 * split: everything a human says looks one way, everything the machine reports
 * looks another. Consent is speech, so it gets the speech treatment.
 */
export default function ConsentMoment({
  customerName,
  agentName,
  onAgentName,
  onConsent,
  onBack,
  busy,
  extra,
}: {
  customerName: string;
  agentName: string;
  onAgentName: (v: string) => void;
  onConsent: () => void;
  onBack: () => void;
  busy: boolean;
  extra?: React.ReactNode;
}) {
  const [declined, setDeclined] = useState(false);

  return (
    <div className="flex flex-1 items-center justify-center overflow-y-auto p-6">
      <div className="w-full max-w-2xl py-6">
        <div className="mb-5 flex items-baseline gap-3">
          <span className="t-label">Before anything else</span>
          <span className="text-[12px]" style={{ color: "var(--graphite)" }}>
            read this to {customerName || "the customer"}
          </span>
        </div>

        <blockquote
          className="card px-7 py-6"
          style={{ borderLeft: "3px solid var(--yourcall)" }}
        >
          <p className="t-speech">
            “Hi, this is {agentName || "…"} calling from PayFlex. Before we start
            — this call may be recorded and I'm using an AI assistant to help me
            pull up accurate information while we talk. Is that alright with
            you?”
          </p>
        </blockquote>

        <p
          className="mt-4 max-w-xl text-[12.5px] leading-relaxed"
          style={{ color: "var(--graphite)" }}
        >
          Nothing runs until this is on record. The co-pilot doesn't warn about
          missing consent — it refuses to process a single turn without it.
        </p>

        {!declined ? (
          <div className="mt-6">
            <label className="t-label mb-1.5 block" htmlFor="agent-name">
              Your name
            </label>
            <input
              id="agent-name"
              value={agentName}
              onChange={(e) => onAgentName(e.target.value)}
              className="card mb-4 w-full max-w-xs px-3 py-2 text-[13px]"
              style={{ fontFamily: "var(--ui)" }}
            />

            {extra}

            <div className="mt-5 flex flex-wrap items-center gap-3">
              <button
                onClick={onConsent}
                disabled={busy}
                className="btn btn-primary"
              >
                {busy ? "Opening the line…" : "They said yes — start the call"}
              </button>
              <button
                onClick={() => setDeclined(true)}
                className="btn btn-quiet"
              >
                They said no
              </button>
              <button
                onClick={onBack}
                className="text-[12px] underline"
                style={{ color: "var(--graphite)" }}
              >
                back
              </button>
            </div>
          </div>
        ) : (
          <div
            className="card mt-6 px-5 py-4"
            style={{ borderLeft: "3px solid var(--halt)" }}
          >
            <p className="text-[13px] font-semibold" style={{ color: "var(--halt)" }}>
              Then the co-pilot stays off for this call.
            </p>
            <p
              className="mt-2 max-w-xl text-[12.5px] leading-relaxed"
              style={{ color: "var(--graphite)" }}
            >
              Carry on without recording or assistance, or offer to call back on
              a non-recorded line. Say this: “That's completely fine — I can
              carry on without the recording, or call you back on a
              non-recorded line, whichever you prefer.”
            </p>
            <button
              onClick={() => setDeclined(false)}
              className="btn btn-quiet mt-4"
            >
              Go back
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

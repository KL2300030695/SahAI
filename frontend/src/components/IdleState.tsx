import type { CallSummary } from "../lib/types";

/**
 * Between calls.
 *
 * A moment of orientation, not a blank page. Three things: the line is open and
 * listening, what the co-pilot will do when someone speaks, and the opening
 * words already in front of the agent so they aren't composing them at the
 * moment the call opens.
 */
export default function IdleState({
  calls,
  onScripted,
  onVoice,
  mode,
}: {
  calls: CallSummary[];
  onScripted: (id: string) => void;
  onVoice: () => void;
  mode: string;
}) {
  return (
    <div className="flex flex-1 justify-center overflow-y-auto p-6">
      <div className="w-full max-w-4xl py-4">
        <div className="mb-1 flex items-center gap-2.5">
          <span className="live-dot" aria-hidden="true" />
          <span className="t-label">Line open · waiting</span>
          {mode && (
            <span className={`tag ${mode === "live" ? "tag-verified" : "tag-yourcall"}`}>
              {mode === "live" ? "connected" : "offline mode"}
            </span>
          )}
        </div>

        <h1 className="t-speech mt-3 max-w-2xl">
          When they start talking, I'll put the next thing to say right here.
        </h1>
        <p
          className="mt-3 max-w-2xl text-[13px] leading-relaxed"
          style={{ color: "var(--graphite)" }}
        >
          Every figure I suggest is traced back to the current handbook and
          marked in the sentence, so you can see what's checked before you say
          it. Anything about credit terms comes to you to confirm — I don't
          settle those.
        </p>

        {/* the opening words, ready before the call opens */}
        <blockquote
          className="card mt-6 px-6 py-5"
          style={{ borderLeft: "3px solid var(--yourcall)" }}
        >
          <span className="t-label">You open with</span>
          <p className="t-speech-sm mt-2">
            “Hi, this is … calling from PayFlex. Before we start — this call may
            be recorded and I'm using an AI assistant to help me pull up accurate
            information while we talk. Is that alright with you?”
          </p>
        </blockquote>

        {/* start a call */}
        <div className="mt-8">
          <span className="t-label">Start a call</span>
          <div className="mt-3">
            <button
              onClick={onVoice}
              className="card px-4 py-3.5 text-left transition-colors hover:border-[color:var(--ink)]"
            >
              <div className="mb-1 flex items-center gap-2 text-[13px] font-semibold">
                Microphone
                <span className="tag tag-verified">live</span>
              </div>
              <p className="text-[12.5px] leading-snug" style={{ color: "var(--graphite)" }}>
                Speak and I'll follow along. Best on speakerphone — see the note
                once you're in.
              </p>
            </button>
          </div>
        </div>

        {/* rehearsal */}
        <div className="mt-8">
          <span className="t-label">Or rehearse on a past call</span>
          <div className="mt-3 space-y-2">
            {calls.map((c) => (
              <button
                key={c.call_id}
                onClick={() => onScripted(c.call_id)}
                className="card block w-full px-4 py-3 text-left transition-colors hover:border-[color:var(--ink)]"
              >
                <div className="mb-1 flex items-center justify-between gap-3">
                  <span className="t-data">{c.call_id}</span>
                  <span
                    className={`tag ${
                      c.outcome === "converted" ? "tag-verified" : "tag-yourcall"
                    }`}
                  >
                    {c.outcome.replace(/_/g, " ")}
                  </span>
                </div>
                <p
                  className="text-[12.5px] leading-snug"
                  style={{ color: "var(--graphite)" }}
                >
                  {c.scenario}
                </p>
              </button>
            ))}
            {!calls.length && (
              <p className="text-[12.5px]" style={{ color: "var(--graphite)" }}>
                Loading…
              </p>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

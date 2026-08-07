import type { GroundedSpan, TurnAssist } from "../lib/types";

/**
 * The Say Line — the signature element.
 *
 * One sentence, set in serif at speaking size, because the agent is about to
 * read it out loud. It is the only large type in the product; everything else
 * on screen exists to answer "can I trust this line?"
 *
 * Its signature behaviour is **inline provenance**: every figure traced to a
 * cited knowledge-base chunk is marked *within the sentence itself* rather than
 * in a citations panel underneath. The offsets come from the same function the
 * grounding guardrail uses to decide whether to block, so the marks and the
 * verdict can never disagree.
 *
 * That teaches the agent the habit the guardrail enforces in code: marked
 * figures are sourced, unmarked ones are not, and anything unsourced never
 * reaches this component at all.
 */

type State = "ready" | "yourcall" | "held" | "listening";

function stateOf(a: TurnAssist | null): State {
  if (!a) return "listening";
  if (a.blocked || !a.nba) return "held";
  return a.nba.requires_human_confirmation ? "yourcall" : "ready";
}

/** Split the sentence into plain text and marked figures. */
function segment(text: string, spans: GroundedSpan[]) {
  const ordered = [...spans]
    .filter((s) => s.start >= 0 && s.end <= text.length && s.end > s.start)
    .sort((a, b) => a.start - b.start);

  const out: Array<{ text: string; span?: GroundedSpan }> = [];
  let cursor = 0;
  for (const s of ordered) {
    if (s.start < cursor) continue; // defensive: never render overlapping marks
    if (s.start > cursor) out.push({ text: text.slice(cursor, s.start) });
    out.push({ text: text.slice(s.start, s.end), span: s });
    cursor = s.end;
  }
  if (cursor < text.length) out.push({ text: text.slice(cursor) });
  return out;
}

export default function SayLine({
  assist,
  thinking,
}: {
  assist: TurnAssist | null;
  thinking: boolean;
}) {
  const state = thinking && assist ? "ready" : stateOf(assist);
  const say = assist?.nba?.say ?? "";
  const spans = assist?.guardrail?.grounded_spans ?? [];
  const parts = segment(say, spans);

  const edge =
    state === "yourcall"
      ? "var(--yourcall)"
      : state === "held"
        ? "var(--halt)"
        : "transparent";

  return (
    <section
      aria-live="polite"
      aria-label="What to say next"
      className="card relative overflow-hidden px-6 py-5"
      style={{ borderLeft: `3px solid ${edge}` }}
    >
      <header className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <span className="t-label">Say next</span>

        <div className="flex flex-wrap items-center gap-1.5">
          {/* The traced count shows even when confirmation is needed. That is
              precisely when the agent most wants to know the numbers are
              checked — hiding it behind the amber state would drop the useful
              half of the message. */}
          {spans.length > 0 && state !== "held" && (
            <span className="tag tag-verified">
              {spans.length} figure{spans.length > 1 ? "s" : ""} traced
            </span>
          )}
          {state === "yourcall" && (
            <span className="tag tag-yourcall">
              You confirm this before saying it
            </span>
          )}
          {state === "held" && <span className="tag tag-halt">Held</span>}
        </div>
      </header>

      {/* --- listening ------------------------------------------------- */}
      {state === "listening" && (
        <p
          className="t-speech"
          style={{ color: "var(--graphite)", opacity: 0.5 }}
        >
          Listening — I'll put a line here when they've finished speaking.
        </p>
      )}

      {/* --- held: say why, plainly, in the interface's voice ----------- */}
      {state === "held" && (
        <div className="settle">
          <p className="t-speech" style={{ color: "var(--halt)" }}>
            I'm holding this one back.
          </p>
          <p
            className="mt-2 max-w-3xl text-[13px] leading-relaxed"
            style={{ color: "var(--graphite)" }}
          >
            {assist?.guardrail?.blocked_reason ??
              "A check didn't pass, so I'd rather say nothing than say the wrong thing."}
          </p>
          <p className="mt-2 text-[13px]" style={{ color: "var(--ink)" }}>
            Ask them to hold for a moment while you check it.
          </p>
        </div>
      )}

      {/* --- the line -------------------------------------------------- */}
      {(state === "ready" || state === "yourcall") && (
        <div
          key={say}
          className="settle"
          style={{ opacity: thinking ? 0.45 : 1, transition: "opacity 160ms" }}
        >
          <p className="t-speech max-w-4xl">
            <span aria-hidden="true" style={{ color: "var(--graphite)" }}>
              “
            </span>
            {parts.map((p, i) =>
              p.span ? (
                <mark
                  key={i}
                  tabIndex={0}
                  className="grounded bg-transparent"
                  style={{ color: "inherit" }}
                  title={`${p.span.doc_title} · ${p.span.version} · ${p.span.chunk_id}`}
                >
                  {p.text}
                </mark>
              ) : (
                <span key={i}>{p.text}</span>
              ),
            )}
            <span aria-hidden="true" style={{ color: "var(--graphite)" }}>
              ”
            </span>
          </p>

          {assist?.nba?.why && (
            <p
              className="mt-3 max-w-3xl text-[12.5px] leading-relaxed"
              style={{ color: "var(--graphite)" }}
            >
              {assist.nba.why}
            </p>
          )}

          {state === "yourcall" && (
            <p
              className="mt-3 max-w-3xl text-[12.5px] leading-relaxed"
              style={{ color: "var(--yourcall)" }}
            >
              This touches credit terms. I can't finalise those — read it, decide,
              and say it in your own words.
            </p>
          )}
        </div>
      )}

      {thinking && (
        <p className="mt-3 text-[11px]" style={{ color: "var(--graphite)" }}>
          Working on the next line — keep going with this one.
        </p>
      )}
    </section>
  );
}

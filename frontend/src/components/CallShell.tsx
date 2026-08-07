import { useEffect, useRef, useState } from "react";
import type {
  CheckOut,
  CostLedger,
  TranscriptTurn,
  TurnAssist,
} from "../lib/types";
import VoiceControl from "./VoiceControl";

/* ---------------------------------------------------------------------------
   The pieces around the Say Line. Each is deliberately quieter than it —
   they exist to answer "can I trust that line?", not to compete with it.
--------------------------------------------------------------------------- */

/** Thin always-present strip: who you're on with, and for how long. */
export function OnAirStrip({
  name,
  detail,
  live,
  source,
}: {
  name: string;
  detail?: string;
  live: boolean;
  source?: string;
}) {
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    if (!live) return;
    const id = setInterval(() => setElapsed((s) => s + 1), 1000);
    return () => clearInterval(id);
  }, [live]);

  const mm = String(Math.floor(elapsed / 60)).padStart(2, "0");
  const ss = String(elapsed % 60).padStart(2, "0");

  return (
    <div className="flex items-center gap-3 px-1 py-2">
      {live && <span className="live-dot shrink-0" aria-hidden="true" />}
      <span className="text-[13px] font-semibold">{name}</span>
      {detail && (
        <span className="text-[12px]" style={{ color: "var(--graphite)" }}>
          {detail}
        </span>
      )}
      {source && <span className="tag">{source}</span>}
      <span className="ml-auto flex items-center gap-3">
        <VoiceControl />
        <span className="t-data" style={{ color: "var(--graphite)" }}>
          {live ? `${mm}:${ss}` : "—"}
        </span>
      </span>
    </div>
  );
}

/** The conversation. Customer turns carry more weight than the agent's own —
 *  the agent already knows what they said. */
export function Conversation({
  turns,
  assists,
  thinkingOn,
}: {
  turns: TranscriptTurn[];
  assists: TurnAssist[];
  thinkingOn: number | null;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    ref.current?.scrollTo({ top: ref.current.scrollHeight, behavior: "smooth" });
  }, [turns.length, thinkingOn]);

  const intentFor = (i: number) =>
    assists.find((a) => a.turn.index === i)?.intent?.intent;

  return (
    <section className="card flex min-h-0 flex-1 flex-col">
      <header className="border-b px-4 py-2.5" style={{ borderColor: "var(--hairline)" }}>
        <span className="t-label">Conversation</span>
      </header>
      <div ref={ref} className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
        {turns.map((t) => {
          const customer = t.speaker === "customer";
          const intent = customer ? intentFor(t.index) : undefined;
          return (
            <div key={`${t.index}-${t.ts}`} className="arrive">
              <div className="mb-1 flex items-center gap-2">
                <span className="t-label" style={{ letterSpacing: "0.08em" }}>
                  {customer ? "customer" : "you"}
                </span>
                {intent && <span className="tag">{intent.replace(/_/g, " ")}</span>}
              </div>
              <p
                className="t-transcript"
                style={{
                  color: customer ? "var(--ink)" : "var(--graphite)",
                  fontWeight: customer ? 400 : 400,
                }}
              >
                {t.text}
              </p>
            </div>
          );
        })}

        {thinkingOn !== null && (
          <p className="t-transcript" style={{ color: "var(--graphite)" }}>
            <span className="live-dot mr-2 inline-block" aria-hidden="true" />
            reading that…
          </p>
        )}

        {turns.length === 0 && (
          <p className="text-[13px]" style={{ color: "var(--graphite)" }}>
            Nothing said yet.
          </p>
        )}
      </div>
    </section>
  );
}

/** The trust column: what the system read, and what it read it from. */
export function WhatThisUsed({ assist }: { assist: TurnAssist | null }) {
  const cited = new Set(assist?.nba?.cited_chunk_ids ?? []);
  const used = (assist?.retrieval?.citations ?? []).filter((c) =>
    cited.has(c.chunk_id),
  );
  const rest = (assist?.retrieval?.citations ?? []).filter(
    (c) => !cited.has(c.chunk_id),
  );
  const dropped = assist?.retrieval?.dropped_stale ?? [];
  const weak =
    assist?.intent && assist.intent.confidence > 0 && assist.intent.confidence < 0.6;

  return (
    <section className="card flex min-h-0 flex-1 flex-col">
      <header className="border-b px-4 py-2.5" style={{ borderColor: "var(--hairline)" }}>
        <span className="t-label">What this used</span>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {!assist?.intent && (
          <p className="p-4 text-[13px]" style={{ color: "var(--graphite)" }}>
            Nothing to show until they speak.
          </p>
        )}

        {assist?.intent && (
          <div
            className="space-y-2 border-b p-4"
            style={{ borderColor: "var(--hairline)" }}
          >
            <Row label="reading" value={assist.intent.intent.replace(/_/g, " ")} />
            <Row label="tone" value={assist.intent.sentiment} />
            {assist.intent.dropoff_risk >= 0.6 && (
              <Row
                label="risk"
                value={`${Math.round(assist.intent.dropoff_risk * 100)}% likely to drop off`}
                tone="yourcall"
              />
            )}
            {weak && (
              <p className="pt-1 text-[12px]" style={{ color: "var(--yourcall)" }}>
                Weak read — I'm not confident this is what they're asking. Treat
                the line as a starting point.
              </p>
            )}
            {assist.intent.buying_signals.length > 0 && (
              <div
                className="rounded-md px-2.5 py-2"
                style={{ background: "var(--verified-wash)" }}
              >
                <div className="t-label mb-1" style={{ color: "var(--verified)" }}>
                  They're ready — move to close
                </div>
                {assist.intent.buying_signals.map((s, i) => (
                  <p key={i} className="text-[12.5px]" style={{ color: "var(--verified)" }}>
                    “{s}”
                  </p>
                ))}
              </div>
            )}
          </div>
        )}

        {dropped.length > 0 && (
          <div
            className="border-b px-4 py-2.5"
            style={{
              borderColor: "var(--hairline)",
              background: "var(--yourcall-wash)",
            }}
          >
            <p className="text-[12px]" style={{ color: "var(--yourcall)" }}>
              Ignored {dropped.length} expired document
              {dropped.length > 1 ? "s" : ""} — those terms changed.
            </p>
          </div>
        )}

        {[...used, ...rest].map((c) => {
          const isUsed = cited.has(c.chunk_id);
          return (
            <article
              key={c.chunk_id}
              className="border-b px-4 py-3 last:border-0"
              style={{
                borderColor: "var(--hairline)",
                background: isUsed ? "var(--verified-wash)" : undefined,
              }}
            >
              <div className="mb-1 flex items-baseline justify-between gap-2">
                <span className="text-[12.5px] font-semibold">{c.title}</span>
                {isUsed && <span className="tag tag-verified shrink-0">quoted</span>}
              </div>
              <div className="t-data mb-1.5" style={{ color: "var(--graphite)" }}>
                {c.version} · {c.chunk_id}
              </div>
              <p
                className="text-[12.5px] leading-relaxed"
                style={{ color: "var(--graphite)" }}
              >
                {c.text.replace(/^#+\s*/gm, "").slice(0, 180)}
                {c.text.length > 180 ? "…" : ""}
              </p>
            </article>
          );
        })}

        {assist && !assist.retrieval?.citations.length && (
          <p className="p-4 text-[12.5px]" style={{ color: "var(--yourcall)" }}>
            No strong match in the handbook for this one — I haven't quoted any
            figures.
          </p>
        )}
      </div>
    </section>
  );
}

function Row({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "yourcall";
}) {
  return (
    <div className="flex items-baseline gap-3">
      <span className="t-label w-14 shrink-0">{label}</span>
      <span
        className="text-[13px] capitalize"
        style={{ color: tone === "yourcall" ? "var(--yourcall)" : "var(--ink)" }}
      >
        {value}
      </span>
    </div>
  );
}

/**
 * Evidence strip — guardrails and cost.
 *
 * Collapsed by default and pinned to the bottom, because the agent will never
 * look at it mid-call. It is here for the reviewer, and expands into the whole
 * safety-and-cost story on demand.
 */
export function EvidenceStrip({
  guard,
  ledger,
  frontierUsd,
}: {
  guard: CheckOut | null;
  ledger: CostLedger | null;
  frontierUsd: number;
}) {
  const [open, setOpen] = useState(false);
  const checks = guard?.checks ?? [];
  const codeChecks = checks.filter((c) => c.enforced_by === "code").length;
  const failed = checks.filter((c) => !c.passed).length;
  const reduction =
    ledger && ledger.total_usd > 0 ? frontierUsd / ledger.total_usd : 0;

  return (
    <section
      className="card overflow-hidden"
      style={{ borderColor: "var(--hairline)" }}
    >
      <button
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex w-full items-center gap-4 px-4 py-2.5 text-left"
      >
        <span
          className="text-[12.5px]"
          style={{ color: failed ? "var(--halt)" : "var(--verified)" }}
        >
          {/* States what the checks found, not what was done about it.
              This used to read "N checks stopped this" whenever anything
              failed — and appeared above a call that had been signed off and
              written, because a failed check on an internal summary blocks
              nothing customer-facing. A strip that announces a stoppage that
              did not happen is worse than one that says less: the Say Line and
              the approval band already report consequences, and they are the
              two places that actually know. */}
          {checks.length
            ? failed
              ? `${failed} check${failed > 1 ? "s" : ""} failed`
              : `${checks.length} checks passed`
            : "Checks idle"}
        </span>
        {checks.length > 0 && (
          <span className="text-[12px]" style={{ color: "var(--graphite)" }}>
            {codeChecks} enforced in code
          </span>
        )}
        {ledger && (
          <span className="t-data ml-auto" style={{ color: "var(--graphite)" }}>
            ${ledger.total_usd.toFixed(6)}
            {reduction > 1 && (
              <span style={{ color: "var(--verified)" }}>
                {" "}
                · {reduction.toFixed(0)}× cheaper
              </span>
            )}
          </span>
        )}
        <span
          className="ml-3 text-[11px]"
          style={{ color: "var(--graphite)" }}
          aria-hidden="true"
        >
          {open ? "hide" : "details"}
        </span>
      </button>

      {open && (
        <div
          className="grid gap-0 border-t md:grid-cols-2"
          style={{ borderColor: "var(--hairline)" }}
        >
          <div className="border-b md:border-b-0 md:border-r" style={{ borderColor: "var(--hairline)" }}>
            {checks.map((c, i) => (
              <div
                key={i}
                className="flex gap-2.5 border-b px-4 py-2 last:border-0"
                style={{ borderColor: "var(--hairline)" }}
              >
                <span
                  className="mt-0.5 text-[12px] font-semibold"
                  style={{ color: c.passed ? "var(--verified)" : "var(--halt)" }}
                >
                  {c.passed ? "✓" : "✗"}
                </span>
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="text-[12.5px] font-medium">
                      {c.name.replace(/_/g, " ")}
                    </span>
                    <span
                      className={`tag ${c.enforced_by === "code" ? "tag-verified" : ""}`}
                    >
                      {c.enforced_by}
                    </span>
                  </div>
                  <p className="text-[11.5px]" style={{ color: "var(--graphite)" }}>
                    {c.detail}
                  </p>
                </div>
              </div>
            ))}
          </div>

          <div className="max-h-64 overflow-y-auto">
            <table className="w-full">
              <tbody className="t-data">
                {(ledger?.decisions ?? []).map((d, i) => (
                  <tr
                    key={i}
                    className="border-b last:border-0"
                    style={{ borderColor: "var(--hairline)" }}
                    title={d.escalation_trigger ?? ""}
                  >
                    <td className="px-4 py-1.5" style={{ color: "var(--graphite)" }}>
                      {d.turn_index ?? "—"}
                    </td>
                    <td className="py-1.5">{d.agent}</td>
                    <td className="py-1.5" style={{ color: "var(--graphite)" }}>
                      {d.tier}
                    </td>
                    <td
                      className="px-4 py-1.5 text-right"
                      style={{ color: "var(--graphite)" }}
                    >
                      {d.usd.toFixed(6)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </section>
  );
}

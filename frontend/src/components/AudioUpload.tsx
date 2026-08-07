import { useRef, useState } from "react";
import { api } from "../lib/api";
import type { CostLedger, TranscriptTurn, TurnAssist } from "../lib/types";

/**
 * Upload a recorded clip and run it through the full pipeline.
 *
 * The fallback when a microphone isn't available — a locked-down laptop, a
 * noisy room, a browser without MediaRecorder — and the path for reviewing an
 * already-recorded call. Same consent gate, same guardrails as the live socket.
 *
 * This one keeps a speaker choice, unlike the microphone: picking a file is a
 * deliberate act, and nobody is mid-sentence while doing it.
 */
export default function AudioUpload({
  callId,
  onTurn,
  onAssist,
  onLedger,
}: {
  callId: string;
  onTurn: (t: TranscriptTurn) => void;
  onAssist: (a: TurnAssist) => void;
  onLedger: (l: CostLedger, frontier: number) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [last, setLast] = useState<{ seconds: number; usd: number } | null>(null);
  const [speaker, setSpeaker] = useState<"customer" | "agent">("customer");
  const inputRef = useRef<HTMLInputElement | null>(null);

  async function handle(file: File) {
    setBusy(true);
    setError(null);
    try {
      const r = await api.audioTurn(callId, file, speaker);
      onTurn(r.assist.turn);
      onAssist(r.assist);
      onLedger(r.ledger, r.frontier_usd ?? 0);
      setLast({
        seconds: r.audio_seconds,
        usd: (r.audio_seconds / 3600) * 0.04, // whisper-large-v3-turbo
      });
    } catch (e: any) {
      setError(String(e.message ?? e));
    } finally {
      setBusy(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  }

  return (
    <section className="card overflow-hidden">
      <header
        className="border-b px-4 py-2.5"
        style={{ borderColor: "var(--hairline)" }}
      >
        <span className="t-label">Or drop in a recording</span>
      </header>

      <div className="space-y-3 p-4">
        <div className="flex gap-1.5">
          {(["customer", "agent"] as const).map((s) => (
            <button
              key={s}
              onClick={() => setSpeaker(s)}
              className="btn flex-1 capitalize"
              style={
                speaker === s
                  ? { background: "var(--ink)", color: "var(--surface)" }
                  : {
                      border: "1px solid var(--hairline)",
                      color: "var(--graphite)",
                    }
              }
            >
              {s}
            </button>
          ))}
        </div>

        <input
          ref={inputRef}
          type="file"
          accept="audio/*,.wav,.mp3,.m4a,.webm,.ogg,.flac"
          disabled={busy}
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) handle(f);
          }}
          className="card w-full px-3 py-2 text-[12.5px]"
        />

        {busy && (
          <p className="text-[12.5px]" style={{ color: "var(--graphite)" }}>
            Writing it down and reading it…
          </p>
        )}
        {error && (
          <p
            className="rounded-md px-3 py-2 text-[12.5px]"
            style={{ background: "var(--halt-wash)", color: "var(--halt)" }}
          >
            {error}
          </p>
        )}
        {last && !busy && (
          <p className="t-data" style={{ color: "var(--graphite)" }}>
            {last.seconds.toFixed(1)}s · ${last.usd.toFixed(6)}
          </p>
        )}
      </div>
    </section>
  );
}

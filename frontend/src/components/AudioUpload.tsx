import { useRef, useState } from "react";
import { api } from "../lib/api";
import type { CostLedger, TranscriptTurn, TurnAssist } from "../lib/types";

/**
 * Upload a recorded clip and run it through the full pipeline.
 *
 * The fallback when a microphone is unavailable — a locked-down laptop, a
 * conference room, a browser without MediaRecorder — and the path for analysing
 * an already-recorded call. Same consent gate, same orchestrator, same
 * guardrails as the live socket.
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
        // whisper-large-v3-turbo bills $0.04 per hour of audio
        usd: (r.audio_seconds / 3600) * 0.04,
      });
    } catch (e: any) {
      setError(String(e.message ?? e));
    } finally {
      setBusy(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  }

  return (
    <div className="panel">
      <div className="panel-title">Audio upload</div>
      <div className="space-y-2.5 px-3 py-3">
        <p className="text-[11px] leading-snug text-slate-500">
          Drop a recorded clip in. It is transcribed with Whisper and run through
          the same pipeline as a live turn.
        </p>

        <div className="flex gap-1">
          {(["customer", "agent"] as const).map((s) => (
            <button
              key={s}
              onClick={() => setSpeaker(s)}
              className={`flex-1 rounded px-2 py-1 text-[11px] font-medium capitalize transition ${
                speaker === s
                  ? "bg-sky-600 text-white"
                  : "border border-slate-800 text-slate-400 hover:bg-slate-800"
              }`}
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
          className="w-full cursor-pointer rounded border border-slate-800 bg-slate-950 px-2 py-1.5 text-[11px] text-slate-400 file:mr-2 file:rounded file:border-0 file:bg-slate-800 file:px-2 file:py-1 file:text-[11px] file:text-slate-200 hover:file:bg-slate-700 disabled:opacity-40"
        />

        {busy && (
          <p className="text-[11px] text-sky-400">Transcribing and analysing…</p>
        )}
        {error && (
          <p className="rounded border border-rose-900/50 bg-rose-950/30 px-2 py-1.5 text-[11px] text-rose-200/80">
            {error}
          </p>
        )}
        {last && !busy && (
          <p className="text-[10px] text-slate-600">
            {last.seconds.toFixed(1)}s of audio · ${last.usd.toFixed(6)} to
            transcribe
          </p>
        )}
        <p className="text-[10px] leading-snug text-slate-600">
          wav · mp3 · m4a · webm · ogg · flac — $0.04 per hour of audio
        </p>
      </div>
    </div>
  );
}

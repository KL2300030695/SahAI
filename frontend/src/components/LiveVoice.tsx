import { useCallback, useEffect, useRef, useState } from "react";
import { useMic, useSpeech } from "../lib/useMic";
import type { CostLedger, TranscriptTurn, TurnAssist } from "../lib/types";

type Speaker = "customer" | "agent";

export interface LiveVoiceHandle {
  turns: TranscriptTurn[];
  assists: TurnAssist[];
}

/**
 * Live microphone co-pilot.
 *
 * One microphone cannot separate the agent from the customer, and Whisper does
 * not diarise. Rather than guess, the UI asks who is speaking — and says so.
 * Getting attribution wrong would silently poison intent detection on every
 * later turn, which is worse than an explicit toggle.
 */
export default function LiveVoice({
  callId,
  onTurn,
  onAssist,
  onLedger,
  onEnd,
}: {
  callId: string;
  onTurn: (t: TranscriptTurn) => void;
  onAssist: (a: TurnAssist) => void;
  onLedger: (l: CostLedger, frontier: number) => void;
  onEnd: () => void;
}) {
  const [speaker, setSpeaker] = useState<Speaker>("customer");
  const [connected, setConnected] = useState(false);
  const [status, setStatus] = useState<string>("");
  const [sttError, setSttError] = useState<string | null>(null);
  const [utterances, setUtterances] = useState(0);
  const wsRef = useRef<WebSocket | null>(null);
  const speech = useSpeech();

  // --- socket ---------------------------------------------------------
  useEffect(() => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/live/${callId}`);
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;

    ws.onopen = () => setConnected(true);
    ws.onclose = () => setConnected(false);
    ws.onerror = () => setSttError("Connection to the co-pilot dropped.");
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      switch (msg.type) {
        case "ready":
          setStatus(`ready · ${msg.stt_model}`);
          break;
        case "transcribing":
          setStatus("transcribing…");
          break;
        case "transcript":
          setStatus("");
          onTurn(msg.turn);
          break;
        case "transcript_skipped":
          setStatus("(silence — skipped)");
          break;
        case "thinking":
          setStatus("analysing…");
          break;
        case "assist":
          setStatus("");
          onAssist(msg.assist);
          if (msg.assist?.nba?.say && !msg.assist.blocked) {
            speech.speak(msg.assist.nba.say);
          }
          break;
        case "ledger":
          onLedger(msg.ledger, msg.frontier_usd ?? 0);
          break;
        case "stt_error":
          setSttError(msg.message);
          break;
        case "blocked":
          setSttError(msg.message ?? msg.reason);
          break;
      }
    };

    return () => ws.close();
    // callId is the identity of this session; re-running on callbacks would
    // tear down a live socket mid-call.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [callId]);

  // --- mic ------------------------------------------------------------
  const handleUtterance = useCallback(
    (blob: Blob, _ms: number) => {
      const ws = wsRef.current;
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      ws.send(JSON.stringify({ action: "speaker", speaker }));
      blob.arrayBuffer().then((buf) => {
        if (ws.readyState === WebSocket.OPEN) ws.send(buf);
        setUtterances((n) => n + 1);
      });
    },
    [speaker],
  );

  const mic = useMic({ onUtterance: handleUtterance });

  function endCall() {
    mic.stop();
    speech.cancel();
    wsRef.current?.send(JSON.stringify({ action: "end" }));
    onEnd();
  }

  const levelPct = Math.min(100, Math.round(mic.level * 900));

  return (
    <div className="panel border-sky-900/50">
      <div className="panel-title flex items-center justify-between border-sky-900/50">
        <span className="text-sky-400">Live microphone</span>
        <span
          className={`chip ${
            connected
              ? "bg-emerald-500/10 text-emerald-300 ring-emerald-500/30"
              : "bg-slate-700/40 text-slate-400 ring-slate-600"
          }`}
        >
          {connected ? "connected" : "connecting…"}
        </span>
      </div>

      <div className="space-y-3 px-3 py-3">
        {!mic.supported && (
          <p className="rounded border border-amber-800/50 bg-amber-950/30 px-2 py-1.5 text-[11px] text-amber-200/80">
            This browser has no MediaRecorder support. Use the audio-upload tab,
            or Chrome/Edge.
          </p>
        )}

        {mic.error && (
          <p className="rounded border border-rose-900/50 bg-rose-950/30 px-2 py-1.5 text-[11px] text-rose-200/80">
            {mic.error}
          </p>
        )}
        {sttError && (
          <p className="rounded border border-rose-900/50 bg-rose-950/30 px-2 py-1.5 text-[11px] text-rose-200/80">
            {sttError}
          </p>
        )}

        {/* who is talking */}
        <div>
          <div className="mb-1 flex items-center justify-between">
            <span className="text-[11px] text-slate-400">Currently speaking</span>
            <span className="text-[10px] text-slate-600">
              one mic can't separate voices
            </span>
          </div>
          <div className="flex gap-1">
            {(["customer", "agent"] as Speaker[]).map((s) => (
              <button
                key={s}
                onClick={() => setSpeaker(s)}
                className={`flex-1 rounded px-2 py-1.5 text-[11px] font-medium capitalize transition ${
                  speaker === s
                    ? "bg-sky-600 text-white"
                    : "border border-slate-800 text-slate-400 hover:bg-slate-800"
                }`}
              >
                {s}
              </button>
            ))}
          </div>
          <p className="mt-1 text-[10px] leading-snug text-slate-600">
            Only customer turns trigger the pipeline — agent turns are recorded
            as context. Whisper does not diarise, so this is stated rather than
            guessed.
          </p>
        </div>

        {/* level meter */}
        <div>
          <div className="mb-1 flex items-center justify-between">
            <span className="text-[11px] text-slate-400">Input level</span>
            <span
              className={`text-[10px] ${
                mic.speaking ? "text-emerald-400" : "text-slate-600"
              }`}
            >
              {mic.speaking ? "speech detected" : "silence"}
            </span>
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-slate-800">
            <div
              className={`h-full rounded-full transition-[width] duration-75 ${
                mic.speaking ? "bg-emerald-400" : "bg-slate-600"
              }`}
              style={{ width: `${levelPct}%` }}
            />
          </div>
          {status && (
            <p className="mt-1 text-[10px] text-sky-400/80">{status}</p>
          )}
        </div>

        {/* controls */}
        <div className="flex gap-2">
          {!mic.recording ? (
            <button
              onClick={mic.start}
              disabled={!mic.supported || !connected}
              className="flex-1 rounded bg-emerald-600 px-3 py-2 text-xs font-semibold text-white hover:bg-emerald-500 disabled:opacity-40"
            >
              ● Start listening
            </button>
          ) : (
            <button
              onClick={mic.stop}
              className="flex-1 rounded bg-rose-600 px-3 py-2 text-xs font-semibold text-white hover:bg-rose-500"
            >
              ■ Pause
            </button>
          )}
          <button
            onClick={endCall}
            className="rounded border border-slate-700 px-3 py-2 text-xs font-medium text-slate-300 hover:bg-slate-800"
          >
            End call
          </button>
        </div>

        {/* spoken suggestions */}
        <label className="flex cursor-pointer items-start gap-2 rounded border border-slate-800 bg-slate-950/60 px-2 py-1.5">
          <input
            type="checkbox"
            checked={speech.enabled}
            disabled={!speech.supported}
            onChange={(e) => {
              speech.setEnabled(e.target.checked);
              if (!e.target.checked) speech.cancel();
            }}
            className="mt-0.5 accent-sky-500"
          />
          <span className="text-[11px] leading-snug text-slate-400">
            Read suggestions aloud
            {speech.speaking && (
              <span className="ml-1 text-emerald-400">· speaking</span>
            )}
            <span className="block text-[10px] text-slate-600">
              {speech.supported
                ? "Browser speech synthesis — no extra cost or latency. Use an earpiece."
                : "Not supported in this browser."}
            </span>
          </span>
        </label>

        <div className="flex justify-between border-t border-slate-800 pt-2 text-[10px] text-slate-600">
          <span>{utterances} utterance{utterances === 1 ? "" : "s"} sent</span>
          <span>segmented on ~900ms silence</span>
        </div>
      </div>
    </div>
  );
}

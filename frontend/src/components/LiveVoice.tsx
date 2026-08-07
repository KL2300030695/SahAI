import { useCallback, useEffect, useRef, useState } from "react";
import { useMic, useSpeech } from "../lib/useMic";
import type { CostLedger, TranscriptTurn, TurnAssist } from "../lib/types";

/**
 * Live microphone.
 *
 * Microphone input is **always** attributed to the customer, and there is no
 * per-utterance speaker control. An earlier version made the agent pick before
 * each utterance, which is unusable by construction: during a live call the
 * agent is the one talking, so they cannot also be operating a toggle between
 * sentences.
 *
 * The deeper limit is stated rather than designed around. On a real desk the
 * agent wears a headset — the browser hears the agent, and the customer is on
 * the phone line where the browser cannot reach them. Single-microphone capture
 * cannot do two-party attribution at all. It is honest for a speakerphone or a
 * solo demo; the phone path is what works on a real desk.
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
  const [connected, setConnected] = useState(false);
  const [status, setStatus] = useState("");
  const [sttError, setSttError] = useState<string | null>(null);
  const [sent, setSent] = useState(0);
  const [heard, setHeard] = useState(0);
  const [skipped, setSkipped] = useState(0);
  const wsRef = useRef<WebSocket | null>(null);
  const speech = useSpeech();

  useEffect(() => {
    let cancelled = false;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/live/${callId}`);
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;

    ws.onopen = () => {
      if (cancelled) return;
      setConnected(true);
      setSttError(null);
    };
    ws.onclose = () => !cancelled && setConnected(false);
    ws.onerror = () =>
      !cancelled && setSttError("Lost the connection to the co-pilot.");

    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      switch (m.type) {
        case "ready":
          setStatus("");
          break;
        case "transcribing":
          setStatus("writing that down…");
          break;
        case "transcript":
          setStatus("");
          setHeard((n) => n + 1);
          onTurn(m.turn);
          break;
        case "transcript_skipped":
          setSkipped((n) => n + 1);
          setStatus("that was silence — skipped");
          break;
        case "thinking":
          setStatus("reading it…");
          break;
        case "assist":
          setStatus("");
          onAssist(m.assist);
          if (m.assist?.nba?.say && !m.assist.blocked) speech.speak(m.assist.nba.say);
          break;
        case "ledger":
          onLedger(m.ledger, m.frontier_usd ?? 0);
          break;
        case "stt_error":
          setSttError(m.message);
          break;
        case "blocked":
          setSttError(
            m.reason === "consent_not_recorded"
              ? "This call no longer exists — the server restarted. Start a new one."
              : (m.message ?? m.reason),
          );
          break;
      }
    };

    return () => {
      cancelled = true;
      // Closing a CONNECTING socket makes the browser fire an error event.
      if (ws.readyState === WebSocket.OPEN) ws.close();
      else if (ws.readyState === WebSocket.CONNECTING)
        ws.addEventListener("open", () => ws.close());
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [callId]);

  const handleUtterance = useCallback((blob: Blob) => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    blob.arrayBuffer().then((buf) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(buf);
      setSent((n) => n + 1);
    });
  }, []);

  const mic = useMic({ onUtterance: handleUtterance });
  const level = Math.min(100, Math.round(mic.level * 900));

  return (
    <section className="card overflow-hidden">
      <header
        className="flex items-center justify-between border-b px-4 py-2.5"
        style={{ borderColor: "var(--hairline)" }}
      >
        <span className="t-label">Microphone</span>
        <span className={`tag ${connected ? "tag-verified" : ""}`}>
          {connected ? "listening" : "connecting…"}
        </span>
      </header>

      <div className="space-y-3 p-4">
        {(mic.error || sttError) && (
          <p
            className="rounded-md px-3 py-2 text-[12.5px]"
            style={{ background: "var(--halt-wash)", color: "var(--halt)" }}
          >
            {mic.error ?? sttError}
          </p>
        )}
        {!mic.supported && (
          <p
            className="rounded-md px-3 py-2 text-[12.5px]"
            style={{ background: "var(--yourcall-wash)", color: "var(--yourcall)" }}
          >
            This browser can't record. Use the upload box below, or Chrome.
          </p>
        )}

        {/* input level */}
        <div>
          <div className="mb-1.5 flex items-center justify-between">
            <span className="t-label">Input</span>
            <span
              className="text-[11px]"
              style={{ color: mic.speaking ? "var(--verified)" : "var(--graphite)" }}
            >
              {mic.speaking ? "hearing speech" : "quiet"}
            </span>
          </div>
          <div
            className="h-1.5 overflow-hidden rounded-full"
            style={{ background: "var(--hairline)" }}
          >
            <div
              className="h-full rounded-full"
              style={{
                width: `${level}%`,
                background: mic.speaking ? "var(--verified)" : "var(--graphite)",
                transition: "width 80ms linear",
              }}
            />
          </div>
          {status && (
            <p className="mt-1.5 text-[11.5px]" style={{ color: "var(--graphite)" }}>
              {status}
            </p>
          )}
        </div>

        <div className="flex gap-2">
          {!mic.recording ? (
            <button
              onClick={mic.start}
              disabled={!mic.supported || !connected}
              className="btn btn-primary flex-1"
            >
              Start listening
            </button>
          ) : (
            <button onClick={mic.stop} className="btn btn-quiet flex-1">
              Pause
            </button>
          )}
          <button
            onClick={() => {
              mic.stop();
              speech.cancel();
              wsRef.current?.send(JSON.stringify({ action: "end" }));
              onEnd();
            }}
            className="btn btn-quiet"
          >
            End
          </button>
        </div>

        <label className="flex cursor-pointer items-start gap-2.5">
          <input
            type="checkbox"
            checked={speech.enabled}
            disabled={!speech.supported}
            onChange={(e) => {
              speech.setEnabled(e.target.checked);
              if (!e.target.checked) speech.cancel();
            }}
            className="mt-0.5"
          />
          <span className="text-[12.5px] leading-snug">
            Read lines to me
            <span className="block text-[11.5px]" style={{ color: "var(--graphite)" }}>
              Use an earpiece — the customer must not hear it.
            </span>
          </span>
        </label>

        {/* scope note — this is a real limitation, stated */}
        <p
          className="border-t pt-3 text-[11.5px] leading-relaxed"
          style={{ borderColor: "var(--hairline)", color: "var(--graphite)" }}
        >
          Everything the mic hears is treated as the customer, so there's nothing
          to operate mid-call. This assumes speakerphone or a solo run — on a
          headset the browser hears you, not them. Use the phone line for a real
          two-party call.
        </p>

        <p className="t-data" style={{ color: "var(--graphite)" }}>
          {sent} sent · {heard} heard{skipped > 0 && ` · ${skipped} silence`}
        </p>
      </div>
    </section>
  );
}

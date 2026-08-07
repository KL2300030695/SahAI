import { useEffect, useRef, useState } from "react";
import type { CostLedger, TranscriptTurn, TurnAssist } from "../lib/types";
import { useSpeech } from "../lib/useMic";

interface TelephonyConfig {
  ready: boolean;
  voice_webhook: string | null;
  stream_url: string | null;
  signature_verification: boolean;
  default_customer_id: string;
  hint: string;
}

interface ActiveCall {
  call_id: string;
  customer_id: string;
  phone_number: string;
  turns: number;
  ended: boolean;
}

/**
 * Real phone calls.
 *
 * The carrier drives the pipeline over its own socket; the dashboard *observes*
 * rather than driving. That split is why this component connects to
 * `/ws/observe/{id}` instead of sending anything — the agent's browser is not in
 * the audio path at all, which is what makes this work when the agent is on a
 * desk phone or a softphone rather than in the browser.
 */
export default function PhoneCall({
  onTurn,
  onAssist,
  onLedger,
  onAttached,
}: {
  onTurn: (t: TranscriptTurn) => void;
  onAssist: (a: TurnAssist) => void;
  onLedger: (l: CostLedger, frontier: number) => void;
  onAttached: (callId: string) => void;
}) {
  const [config, setConfig] = useState<TelephonyConfig | null>(null);
  const [active, setActive] = useState<ActiveCall[]>([]);
  const [attached, setAttached] = useState<string | null>(null);
  const [from, setFrom] = useState<string>("");
  const [status, setStatus] = useState("");
  const wsRef = useRef<WebSocket | null>(null);
  const speech = useSpeech();

  useEffect(() => {
    fetch("/api/telephony/config")
      .then((r) => r.json())
      .then(setConfig)
      .catch(() => {});
  }, []);

  // Poll for inbound calls. A real deployment would push this over a socket;
  // for a single-operator console a 2s poll is honest and simpler.
  useEffect(() => {
    if (attached) return;
    const tick = () =>
      fetch("/api/telephony/active")
        .then((r) => r.json())
        .then(setActive)
        .catch(() => {});
    tick();
    const id = setInterval(tick, 2000);
    return () => clearInterval(id);
  }, [attached]);

  function attach(callId: string) {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/observe/${callId}`);
    wsRef.current = ws;
    setAttached(callId);
    onAttached(callId);

    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      switch (msg.type) {
        case "attached":
          setFrom(msg.from || "");
          setStatus(`attached · ${msg.backlog} turn(s) already handled`);
          break;
        case "phone_connected":
          setFrom(msg.from || "");
          setStatus("caller connected");
          break;
        case "transcript":
          setStatus("");
          onTurn(msg.turn);
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
        case "call_ended":
          setStatus("caller hung up");
          break;
      }
    };
    ws.onclose = () => setStatus((s) => s || "disconnected");
  }

  useEffect(
    () => () => {
      // Same StrictMode hazard as LiveVoice: closing a CONNECTING socket makes
      // the browser fire an error event. Wait for the handshake first.
      const ws = wsRef.current;
      if (!ws) return;
      if (ws.readyState === WebSocket.OPEN) ws.close();
      else if (ws.readyState === WebSocket.CONNECTING) {
        ws.addEventListener("open", () => ws.close());
      }
    },
    [],
  );

  return (
    <div className="panel border-indigo-900/50">
      <div className="panel-title flex items-center justify-between border-indigo-900/50">
        <span className="text-indigo-400">Phone line</span>
        <span
          className={`chip ${
            attached
              ? "bg-emerald-500/10 text-emerald-300 ring-emerald-500/30"
              : config?.ready
                ? "bg-indigo-500/10 text-indigo-300 ring-indigo-500/30"
                : "bg-amber-500/10 text-amber-300 ring-amber-500/30"
          }`}
        >
          {attached ? "on call" : config?.ready ? "listening" : "not configured"}
        </span>
      </div>

      <div className="space-y-3 px-3 py-3">
        {attached ? (
          <>
            <div className="rounded border border-emerald-800/50 bg-emerald-950/20 px-2 py-2">
              <div className="text-[11px] text-slate-400">Connected caller</div>
              <div className="font-mono text-sm text-emerald-300">
                {from || "unknown number"}
              </div>
              <div className="mt-0.5 font-mono text-[10px] text-slate-600">
                {attached}
              </div>
            </div>
            {status && (
              <p className="text-[11px] text-indigo-400/80">{status}</p>
            )}
            <p className="rounded border border-slate-800 bg-slate-950/60 px-2 py-1.5 text-[10px] leading-snug text-slate-500">
              The carrier separates the two call legs, so the customer and the
              agent arrive on different tracks. Speaker attribution here is
              exact — no toggle, no guessing.
            </p>
          </>
        ) : (
          <>
            {!config?.ready && (
              <div className="rounded border border-amber-800/50 bg-amber-950/30 px-2 py-2">
                <p className="text-[11px] leading-snug text-amber-200/90">
                  No public URL configured, so a carrier cannot reach this
                  server. Set <code className="font-mono">PUBLIC_BASE_URL</code>{" "}
                  to an https tunnel and restart.
                </p>
              </div>
            )}

            {config?.ready && (
              <div className="space-y-1.5">
                <div className="text-[11px] text-slate-400">
                  Point your Twilio number's Voice webhook here
                </div>
                <code className="block break-all rounded border border-slate-800 bg-slate-950 px-2 py-1.5 font-mono text-[10px] text-slate-300">
                  {config.voice_webhook}
                </code>
                {!config.signature_verification && (
                  <p className="text-[10px] leading-snug text-amber-500/80">
                    Signature verification is off — set{" "}
                    <code className="font-mono">TWILIO_AUTH_TOKEN</code> before
                    exposing this publicly.
                  </p>
                )}
              </div>
            )}

            <div>
              <div className="mb-1 flex items-center justify-between">
                <span className="text-[11px] text-slate-400">Inbound calls</span>
                <span className="text-[10px] text-slate-600">polling…</span>
              </div>
              {active.length === 0 ? (
                <p className="rounded border border-slate-800 bg-slate-950/60 px-2 py-3 text-center text-[11px] text-slate-600">
                  Waiting for a call.
                  <span className="mt-1 block text-[10px]">
                    No phone line? Run{" "}
                    <code className="font-mono">python simulate_phone_call.py</code>
                  </span>
                </p>
              ) : (
                <div className="space-y-1">
                  {active.map((c) => (
                    <button
                      key={c.call_id}
                      onClick={() => attach(c.call_id)}
                      className="w-full rounded border border-emerald-800/60 bg-emerald-950/20 px-2 py-2 text-left transition hover:border-emerald-600"
                    >
                      <div className="flex items-center justify-between">
                        <span className="font-mono text-xs text-emerald-300">
                          {c.phone_number || "unknown"}
                        </span>
                        <span className="chip bg-emerald-500/10 text-emerald-300 ring-emerald-500/30">
                          answer
                        </span>
                      </div>
                      <div className="mt-0.5 font-mono text-[10px] text-slate-600">
                        {c.call_id} · {c.turns} turn{c.turns === 1 ? "" : "s"}
                      </div>
                    </button>
                  ))}
                </div>
              )}
            </div>
          </>
        )}

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
            <span className="block text-[10px] text-slate-600">
              Use an earpiece — the caller must not hear the co-pilot.
            </span>
          </span>
        </label>
      </div>
    </div>
  );
}

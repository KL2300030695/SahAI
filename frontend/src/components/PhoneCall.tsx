import { useEffect, useRef, useState } from "react";
import type { CostLedger, TranscriptTurn, TurnAssist } from "../lib/types";
import { useSpeech } from "../lib/useMic";

interface TelephonyConfig {
  ready: boolean;
  voice_webhook: string | null;
  signature_verification: boolean;
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
 * The phone line.
 *
 * The carrier drives the pipeline over its own socket; the dashboard *observes*
 * rather than driving. That split is why this connects to `/ws/observe/{id}`
 * and sends nothing — the agent's browser is not in the audio path at all,
 * which is what makes it work when the agent is on a desk phone.
 *
 * It is also the only path with exact speaker attribution: the carrier keeps
 * each leg separate and labels every frame, so nobody has to declare who is
 * talking.
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
  const [from, setFrom] = useState("");
  const [status, setStatus] = useState("");
  const wsRef = useRef<WebSocket | null>(null);
  const speech = useSpeech();

  useEffect(() => {
    fetch("/api/telephony/config").then((r) => r.json()).then(setConfig).catch(() => {});
  }, []);

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

  useEffect(
    () => () => {
      const ws = wsRef.current;
      if (!ws) return;
      if (ws.readyState === WebSocket.OPEN) ws.close();
      else if (ws.readyState === WebSocket.CONNECTING)
        ws.addEventListener("open", () => ws.close());
    },
    [],
  );

  function attach(callId: string) {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/observe/${callId}`);
    wsRef.current = ws;
    setAttached(callId);
    onAttached(callId);

    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      switch (m.type) {
        case "attached":
        case "phone_connected":
          setFrom(m.from || "");
          setStatus(m.type === "attached" ? "on the call" : "caller connected");
          break;
        case "transcript":
          setStatus("");
          onTurn(m.turn);
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
        case "call_ended":
          setStatus("they hung up");
          break;
      }
    };
  }

  return (
    <section className="card overflow-hidden">
      <header
        className="flex items-center justify-between border-b px-4 py-2.5"
        style={{ borderColor: "var(--hairline)" }}
      >
        <span className="t-label">Phone line</span>
        <span
          className={`tag ${
            attached ? "tag-verified" : config?.ready ? "" : "tag-yourcall"
          }`}
        >
          {attached ? "on a call" : config?.ready ? "waiting" : "not set up"}
        </span>
      </header>

      <div className="space-y-3 p-4">
        {attached ? (
          <>
            <div>
              <span className="t-label">Caller</span>
              <p className="t-data mt-1 text-[14px]">{from || "unknown number"}</p>
            </div>
            {status && (
              <p className="text-[12.5px]" style={{ color: "var(--graphite)" }}>
                {status}
              </p>
            )}
            <p
              className="rounded-md px-3 py-2 text-[11.5px] leading-relaxed"
              style={{ background: "var(--verified-wash)", color: "var(--verified)" }}
            >
              The carrier sends each person on their own line, so I always know
              who's speaking. Nothing to set.
            </p>
          </>
        ) : (
          <>
            {!config?.ready && (
              <p
                className="rounded-md px-3 py-2 text-[12.5px] leading-relaxed"
                style={{ background: "var(--yourcall-wash)", color: "var(--yourcall)" }}
              >
                No public address set, so a carrier can't reach this machine.
                Set PUBLIC_BASE_URL to an https tunnel and restart.
              </p>
            )}

            {config?.ready && (
              <div>
                <span className="t-label">Point your number here</span>
                <p
                  className="t-data mt-1 break-all rounded-md px-2.5 py-2"
                  style={{ background: "var(--paper)" }}
                >
                  {config.voice_webhook}
                </p>
                {!config.signature_verification && (
                  <p className="mt-1.5 text-[11.5px]" style={{ color: "var(--yourcall)" }}>
                    Signature checking is off — set TWILIO_AUTH_TOKEN before
                    exposing this.
                  </p>
                )}
              </div>
            )}

            <div>
              <span className="t-label">Incoming</span>
              {active.length === 0 ? (
                <p
                  className="mt-2 rounded-md px-3 py-3 text-center text-[12.5px]"
                  style={{ background: "var(--paper)", color: "var(--graphite)" }}
                >
                  Nobody on the line.
                  <span className="mt-1 block text-[11.5px]">
                    No phone? Run{" "}
                    <span className="t-data">python simulate_phone_call.py</span>
                  </span>
                </p>
              ) : (
                <div className="mt-2 space-y-2">
                  {active.map((c) => (
                    <button
                      key={c.call_id}
                      onClick={() => attach(c.call_id)}
                      className="card block w-full px-3 py-2.5 text-left"
                      style={{ borderColor: "var(--verified)" }}
                    >
                      <div className="flex items-center justify-between gap-3">
                        <span className="t-data text-[13px]">
                          {c.phone_number || "unknown"}
                        </span>
                        <span className="tag tag-verified">pick up</span>
                      </div>
                      <span className="t-data" style={{ color: "var(--graphite)" }}>
                        {c.turns} turn{c.turns === 1 ? "" : "s"} so far
                      </span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          </>
        )}

        <label className="flex cursor-pointer items-start gap-2.5 border-t pt-3"
          style={{ borderColor: "var(--hairline)" }}>
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
              Earpiece only — the caller must not hear it.
            </span>
          </span>
        </label>
      </div>
    </section>
  );
}

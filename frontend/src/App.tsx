import { useEffect, useMemo, useRef, useState } from "react";
import { api, openCallSocket } from "./lib/api";
import type {
  CallDetail,
  CallSummary,
  CostLedger,
  PostCallResult,
  TranscriptTurn,
  TurnAssist,
} from "./lib/types";
import SayLine from "./components/SayLine";
import {
  Conversation,
  EvidenceStrip,
  OnAirStrip,
  WhatThisUsed,
} from "./components/CallShell";
import ConsentMoment from "./components/ConsentMoment";
import ApprovalBand from "./components/ApprovalBand";
import IdleState from "./components/IdleState";
import LiveVoice from "./components/LiveVoice";
import AudioUpload from "./components/AudioUpload";
import PhoneCall from "./components/PhoneCall";

/**
 * Layout is horizontal bands, not columns.
 *
 * Columns imply the things in them matter equally. They don't: the agent needs
 * one sentence to say, then evidence that it's trustworthy, then — only if they
 * go looking — the machinery. Bands let that hierarchy be literal.
 */

type Phase =
  | "idle"
  | "consent"
  | "scripted"
  | "voice"
  | "phone"
  | "ended"
  | "review";

type Kind = "scripted" | "voice" | "phone";

interface CustomerRow {
  customer_id: string;
  name: string;
  city: string;
  kyc_status: string;
  do_not_call: boolean;
}

export default function App() {
  const [phase, setPhase] = useState<Phase>("idle");
  const [kind, setKind] = useState<Kind>("scripted");
  const [mode, setMode] = useState("");

  const [calls, setCalls] = useState<CallSummary[]>([]);
  const [customers, setCustomers] = useState<CustomerRow[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<CallDetail | null>(null);
  const [agentName, setAgentName] = useState("Priya");
  const [voiceCustomer, setVoiceCustomer] = useState("");

  const [turns, setTurns] = useState<TranscriptTurn[]>([]);
  const [assists, setAssists] = useState<TurnAssist[]>([]);
  const [thinking, setThinking] = useState<number | null>(null);
  const [ledger, setLedger] = useState<CostLedger | null>(null);
  const [frontierUsd, setFrontierUsd] = useState(0);
  const [post, setPost] = useState<PostCallResult | null>(null);
  const [customerBefore, setCustomerBefore] = useState<Record<string, unknown> | null>(null);

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    api.listCalls().then(setCalls).catch((e) => setError(String(e.message)));
    api.health().then((h) => setMode(h.mode)).catch(() => {});
    api.customers().then((c) => {
      setCustomers(c);
      if (c.length) setVoiceCustomer(c[0].customer_id);
    }).catch(() => {});
    return () => closeSocket();
  }, []);

  function closeSocket() {
    const ws = wsRef.current;
    if (!ws) return;
    if (ws.readyState === WebSocket.OPEN) ws.close();
    else if (ws.readyState === WebSocket.CONNECTING) {
      ws.addEventListener("open", () => ws.close());
    }
    wsRef.current = null;
  }

  const latest = useMemo(
    () => (assists.length ? assists[assists.length - 1] : null),
    [assists],
  );
  const live = phase === "scripted" || phase === "voice" || phase === "phone";

  function reset() {
    closeSocket();
    setTurns([]);
    setAssists([]);
    setLedger(null);
    setPost(null);
    setThinking(null);
    setError(null);
    setSelected(null);
    setCustomerBefore(null);
  }

  // ---- entry points -------------------------------------------------
  async function chooseScripted(id: string) {
    reset();
    setKind("scripted");
    setSelected(id);
    setPhase("consent");
    try {
      const d = await api.getCall(id);
      setDetail(d);
      if (d.crm) setAgentName(d.agent_name || agentName);
    } catch (e: any) {
      setError(String(e.message));
    }
  }

  function chooseVoice() {
    reset();
    setKind("voice");
    setDetail(null);
    setPhase("consent");
  }

  function choosePhone() {
    reset();
    setKind("phone");
    setDetail(null);
    setPhase("phone");
  }

  // ---- consent gate --------------------------------------------------
  async function giveConsent() {
    setBusy(true);
    setError(null);
    try {
      if (kind === "scripted" && selected) {
        await api.consent(selected, agentName);
        await snapshotCustomer(selected);
        startScriptedStream(selected);
        setPhase("scripted");
      } else {
        const r = await api.liveStart(voiceCustomer, agentName);
        setSelected(r.call_id);
        await snapshotCustomer(r.call_id);
        setPhase("voice");
      }
    } catch (e: any) {
      setError(String(e.message));
    } finally {
      setBusy(false);
    }
  }

  /** Snapshot the record before the call so approval can show before → after. */
  async function snapshotCustomer(callId: string) {
    try {
      const d = await api.getCall(callId).catch(() => null);
      const cid = d?.customer_id ?? detail?.customer_id ?? voiceCustomer;
      if (cid) setCustomerBefore(await api.customer(cid));
    } catch {
      /* the diff degrades to "—" rather than blocking the call */
    }
  }

  function startScriptedStream(id: string) {
    const ws = openCallSocket(id);
    wsRef.current = ws;
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      switch (m.type) {
        case "turn":
          setTurns((t) => [...t, m.turn]);
          break;
        case "thinking":
          setThinking(m.turn_index);
          break;
        case "assist":
          setThinking(null);
          setAssists((a) => [...a, m.assist]);
          break;
        case "ledger":
          setLedger(m.ledger);
          setFrontierUsd(m.frontier_usd ?? 0);
          break;
        case "blocked":
          setError(m.message ?? m.reason);
          break;
        case "call_ended":
          setPhase("ended");
          break;
      }
    };
  }

  async function finalise() {
    if (!selected) return;
    setBusy(true);
    try {
      const r = await api.finalise(selected);
      setPost(r);
      setLedger(r.ledger);
      setFrontierUsd(r.frontier_usd ?? frontierUsd);
      closeSocket();
      setPhase("review");
    } catch (e: any) {
      setError(String(e.message));
    } finally {
      setBusy(false);
    }
  }

  const who =
    detail?.crm?.name ??
    customers.find((c) => c.customer_id === voiceCustomer)?.name ??
    "Customer";
  const whoDetail =
    detail?.crm
      ? `${detail.crm.city} · KYC ${detail.crm.kyc_status.replace(/_/g, " ")}`
      : customers.find((c) => c.customer_id === voiceCustomer)?.city;

  return (
    <div className="flex h-full flex-col">
      {/* ---------- masthead ---------- */}
      <header
        className="flex shrink-0 items-center justify-between border-b px-5 py-3"
        style={{ borderColor: "var(--hairline)", background: "var(--surface)" }}
      >
        <div className="flex items-baseline gap-3">
          <span className="text-[15px] font-semibold tracking-tight">
            Sah<span style={{ color: "var(--verified)" }}>AI</span>
          </span>
          <span className="text-[12px]" style={{ color: "var(--graphite)" }}>
            Co-pilot for Pay-in-3 calls
          </span>
        </div>
        {phase !== "idle" && (
          <button
            onClick={() => {
              reset();
              setPhase("idle");
            }}
            className="text-[12px] underline"
            style={{ color: "var(--graphite)" }}
          >
            end and start over
          </button>
        )}
      </header>

      {error && (
        <div
          className="shrink-0 border-b px-5 py-2 text-[12.5px]"
          style={{
            borderColor: "var(--hairline)",
            background: "var(--halt-wash)",
            color: "var(--halt)",
          }}
        >
          {error}
        </div>
      )}

      {/* ---------- idle ---------- */}
      {phase === "idle" && (
        <IdleState
          calls={calls}
          mode={mode}
          onScripted={chooseScripted}
          onVoice={chooseVoice}
          onPhone={choosePhone}
        />
      )}

      {/* ---------- consent ---------- */}
      {phase === "consent" && (
        <ConsentMoment
          customerName={who}
          agentName={agentName}
          onAgentName={setAgentName}
          onConsent={giveConsent}
          onBack={() => {
            reset();
            setPhase("idle");
          }}
          busy={busy}
          extra={
            kind === "voice" ? (
              <div>
                <label className="t-label mb-1.5 block" htmlFor="cust">
                  Who you're calling
                </label>
                <select
                  id="cust"
                  value={voiceCustomer}
                  onChange={(e) => setVoiceCustomer(e.target.value)}
                  className="card w-full max-w-sm px-3 py-2 text-[13px]"
                >
                  {customers.map((c) => (
                    <option key={c.customer_id} value={c.customer_id}>
                      {c.name} — {c.city}
                      {c.do_not_call ? " · do not call" : ""}
                    </option>
                  ))}
                </select>
              </div>
            ) : null
          }
        />
      )}

      {/* ---------- the call ---------- */}
      {(live || phase === "ended" || phase === "review") && (
        <main className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-4">
          <OnAirStrip
            name={who}
            detail={whoDetail}
            live={live}
            source={
              kind === "phone" ? "phone" : kind === "voice" ? "microphone" : "rehearsal"
            }
          />

          {phase === "review" && post ? (
            <ApprovalBand
              result={post}
              customerBefore={customerBefore}
              onApproved={() => {}}
            />
          ) : (
            <>
              <SayLine assist={latest} thinking={thinking !== null} />

              <div className="grid min-h-[16rem] flex-1 gap-3 lg:grid-cols-[1.15fr_1fr]">
                <Conversation
                  turns={turns}
                  assists={assists}
                  thinkingOn={thinking}
                />
                <div className="flex min-h-0 flex-col gap-3">
                  <WhatThisUsed assist={latest} />
                  {kind === "voice" && selected && (
                    <>
                      <LiveVoice
                        callId={selected}
                        onTurn={(t) => setTurns((p) => [...p, t])}
                        onAssist={(a) => {
                          setThinking(null);
                          setAssists((p) => [...p, a]);
                        }}
                        onLedger={(l, f) => {
                          setLedger(l);
                          setFrontierUsd(f);
                        }}
                        onEnd={finalise}
                      />
                      <AudioUpload
                        callId={selected}
                        onTurn={(t) => setTurns((p) => [...p, t])}
                        onAssist={(a) => setAssists((p) => [...p, a])}
                        onLedger={(l, f) => {
                          setLedger(l);
                          setFrontierUsd(f);
                        }}
                      />
                    </>
                  )}
                  {kind === "phone" && (
                    <PhoneCall
                      onAttached={(id) => {
                        setSelected(id);
                        snapshotCustomer(id);
                      }}
                      onTurn={(t) => setTurns((p) => [...p, t])}
                      onAssist={(a) => {
                        setThinking(null);
                        setAssists((p) => [...p, a]);
                      }}
                      onLedger={(l, f) => {
                        setLedger(l);
                        setFrontierUsd(f);
                      }}
                    />
                  )}
                </div>
              </div>

              {(phase === "ended" || turns.length > 0) && (
                <button
                  onClick={finalise}
                  disabled={busy || !turns.length || !selected}
                  className="btn btn-primary self-start"
                >
                  {busy
                    ? "Writing it up…"
                    : phase === "ended"
                      ? "They hung up — write it up"
                      : "End the call and write it up"}
                </button>
              )}
            </>
          )}

          <EvidenceStrip
            guard={post ? post.guardrail : (latest?.guardrail ?? null)}
            ledger={ledger}
            frontierUsd={frontierUsd}
          />
        </main>
      )}
    </div>
  );
}

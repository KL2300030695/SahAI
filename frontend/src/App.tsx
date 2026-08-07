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
import AssistPanel from "./components/AssistPanel";
import CostMeter from "./components/CostMeter";
import PostCallReview from "./components/PostCallReview";
import LiveVoice from "./components/LiveVoice";
import AudioUpload from "./components/AudioUpload";
import PhoneCall from "./components/PhoneCall";
import { Empty, Spinner } from "./components/Bits";

type Phase =
  | "mode"
  | "select"
  | "consent"
  | "live"
  | "ended"
  | "review"
  | "voice_setup"
  | "voice_live"
  | "phone";

interface CustomerRow {
  customer_id: string;
  name: string;
  city: string;
  kyc_status: string;
  do_not_call: boolean;
}

export default function App() {
  const [calls, setCalls] = useState<CallSummary[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<CallDetail | null>(null);
  const [phase, setPhase] = useState<Phase>("mode");
  const [agentName, setAgentName] = useState("Priya");

  // voice mode
  const [customers, setCustomers] = useState<CustomerRow[]>([]);
  const [voiceCustomer, setVoiceCustomer] = useState<string>("");
  const [liveCallId, setLiveCallId] = useState<string | null>(null);

  const [turns, setTurns] = useState<TranscriptTurn[]>([]);
  const [assists, setAssists] = useState<TurnAssist[]>([]);
  const [thinking, setThinking] = useState<number | null>(null);
  const [ledger, setLedger] = useState<CostLedger | null>(null);
  const [frontierUsd, setFrontierUsd] = useState(0);
  const [post, setPost] = useState<PostCallResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<string>("");

  const wsRef = useRef<WebSocket | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    api.listCalls().then(setCalls).catch((e) => setError(String(e.message)));
    api.health().then((h) => setMode(h.mode)).catch(() => {});
    return () => wsRef.current?.close();
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo({
      top: scrollRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [turns.length, thinking]);

  const latest = useMemo(
    () => (assists.length ? assists[assists.length - 1] : null),
    [assists],
  );

  async function choose(id: string) {
    setSelected(id);
    setError(null);
    setTurns([]);
    setAssists([]);
    setLedger(null);
    setPost(null);
    setPhase("consent");
    try {
      setDetail(await api.getCall(id));
    } catch (e: any) {
      setError(String(e.message));
    }
  }

  async function giveConsent() {
    if (!selected) return;
    setBusy(true);
    try {
      await api.consent(selected, agentName);
      startStream(selected);
      setPhase("live");
    } catch (e: any) {
      setError(String(e.message));
    } finally {
      setBusy(false);
    }
  }

  function startStream(id: string) {
    const ws = openCallSocket(id);
    wsRef.current = ws;
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      switch (msg.type) {
        case "turn":
          setTurns((t) => [...t, msg.turn]);
          break;
        case "thinking":
          setThinking(msg.turn_index);
          break;
        case "assist":
          setThinking(null);
          setAssists((a) => [...a, msg.assist]);
          break;
        case "ledger":
          setLedger(msg.ledger);
          setFrontierUsd(msg.frontier_usd ?? 0);
          break;
        case "blocked":
          setError(msg.message ?? msg.reason);
          break;
        case "call_ended":
          setPhase("ended");
          break;
      }
    };
    ws.onerror = () => setError("WebSocket error — is the backend running?");
  }

  async function startVoiceCall() {
    if (!voiceCustomer) {
      setError("Pick a customer first.");
      return;
    }
    setBusy(true);
    setError(null);
    setTurns([]);
    setAssists([]);
    setLedger(null);
    setPost(null);
    try {
      const r = await api.liveStart(voiceCustomer, agentName);
      setLiveCallId(r.call_id);
      setSelected(r.call_id);
      setPhase("voice_live");
    } catch (e: any) {
      setError(String(e.message));
    } finally {
      setBusy(false);
    }
  }

  async function finalise() {
    if (!selected) return;
    setBusy(true);
    try {
      const r = await api.finalise(selected);
      setPost(r);
      setLedger(r.ledger);
      setFrontierUsd(r.frontier_usd ?? frontierUsd);
      setPhase("review");
    } catch (e: any) {
      setError(String(e.message));
    } finally {
      setBusy(false);
    }
  }

  const assistFor = (idx: number) => assists.find((a) => a.turn.index === idx);

  return (
    <div className="flex h-screen flex-col">
      {/* ---------- header ---------- */}
      <header className="flex shrink-0 items-center justify-between border-b border-slate-800 bg-slate-900/80 px-4 py-2.5">
        <div className="flex items-baseline gap-3">
          <h1 className="text-sm font-bold tracking-tight text-slate-100">
            Sah<span className="text-emerald-400">AI</span>
          </h1>
          <span className="text-[11px] text-slate-500">
            Voice co-pilot · PayFlex Pay-in-3
          </span>
        </div>
        <div className="flex items-center gap-2">
          {mode && (
            <span
              className={`chip ${
                mode === "live"
                  ? "bg-emerald-500/10 text-emerald-300 ring-emerald-500/30"
                  : "bg-amber-500/10 text-amber-300 ring-amber-500/30"
              }`}
            >
              {mode === "live" ? "live · groq" : "mock"}
            </span>
          )}
          {phase !== "mode" && (
            <button
              onClick={() => {
                wsRef.current?.close();
                setPhase("mode");
                setSelected(null);
                setLiveCallId(null);
                setTurns([]);
                setAssists([]);
                setLedger(null);
                setPost(null);
                setError(null);
              }}
              className="text-[11px] text-slate-500 hover:text-slate-300"
            >
              change call
            </button>
          )}
        </div>
      </header>

      {error && (
        <div className="shrink-0 border-b border-rose-900/50 bg-rose-950/40 px-4 py-1.5 text-[11px] text-rose-300">
          {error}
        </div>
      )}

      {/* ---------- mode picker ---------- */}
      {phase === "mode" && (
        <div className="flex flex-1 items-center justify-center p-6">
          <div className="w-full max-w-2xl">
            <h2 className="mb-1 text-base font-semibold text-slate-200">
              How do you want to run the co-pilot?
            </h2>
            <p className="mb-4 text-xs text-slate-500">
              Both paths run the identical pipeline — same agents, same
              guardrails, same consent gate.
            </p>
            <div className="grid grid-cols-3 gap-3">
              <button
                onClick={() => setPhase("select")}
                className="rounded-lg border border-slate-800 bg-slate-900/60 p-4 text-left transition hover:border-sky-800 hover:bg-slate-900"
              >
                <div className="mb-1 text-sm font-semibold text-slate-100">
                  Scripted call
                </div>
                <p className="text-[11px] leading-snug text-slate-500">
                  Replay one of four seeded transcripts turn by turn. Deterministic
                  — the reliable demo path.
                </p>
              </button>
              <button
                onClick={() => {
                  setPhase("voice_setup");
                  api
                    .customers()
                    .then((c) => {
                      setCustomers(c);
                      if (c.length) setVoiceCustomer(c[0].customer_id);
                    })
                    .catch((e) => setError(String(e.message)));
                }}
                className="rounded-lg border border-emerald-900/60 bg-slate-900/60 p-4 text-left transition hover:border-emerald-700 hover:bg-slate-900"
              >
                <div className="mb-1 flex items-center gap-2 text-sm font-semibold text-slate-100">
                  Live voice
                  <span className="chip bg-emerald-500/10 text-emerald-300 ring-emerald-500/30">
                    mic
                  </span>
                </div>
                <p className="text-[11px] leading-snug text-slate-500">
                  Speak into the microphone. Whisper transcribes each utterance
                  and the co-pilot assists in real time. Audio upload too.
                </p>
              </button>
              <button
                onClick={() => {
                  setTurns([]);
                  setAssists([]);
                  setLedger(null);
                  setPost(null);
                  setError(null);
                  setPhase("phone");
                }}
                className="rounded-lg border border-indigo-900/60 bg-slate-900/60 p-4 text-left transition hover:border-indigo-700 hover:bg-slate-900"
              >
                <div className="mb-1 flex items-center gap-2 text-sm font-semibold text-slate-100">
                  Phone call
                  <span className="chip bg-indigo-500/10 text-indigo-300 ring-indigo-500/30">
                    twilio
                  </span>
                </div>
                <p className="text-[11px] leading-snug text-slate-500">
                  A real inbound call. The carrier sends each party on its own
                  track, so speaker attribution is exact rather than guessed.
                </p>
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ---------- voice setup ---------- */}
      {phase === "voice_setup" && (
        <div className="flex flex-1 items-center justify-center p-6">
          <div className="w-full max-w-lg rounded-lg border border-amber-900/50 bg-slate-900/60 p-5">
            <div className="mb-3 flex items-center gap-2">
              <span className="text-amber-400">⚑</span>
              <h2 className="text-sm font-semibold text-slate-100">
                Mandatory consent disclosure
              </h2>
            </div>
            <blockquote className="mb-4 rounded border-l-2 border-amber-600 bg-slate-950 px-3 py-2.5 text-xs italic leading-relaxed text-slate-300">
              “Hi, this is {agentName} calling from PayFlex. Before we start — this
              call may be recorded and I'm using an AI assistant to help me pull up
              accurate information while we talk. Is that alright with you?”
            </blockquote>
            <div className="mb-4 rounded border border-slate-800 bg-slate-950/60 px-3 py-2">
              <p className="text-[11px] leading-snug text-slate-500">
                The live-audio socket refuses to accept a single byte of
                microphone data until consent is on record — the same code gate
                as the scripted path, not a second one.
              </p>
            </div>

            <label className="mb-1 block text-[11px] text-slate-400">
              Customer
            </label>
            <select
              value={voiceCustomer}
              onChange={(e) => setVoiceCustomer(e.target.value)}
              className="mb-3 w-full rounded border border-slate-800 bg-slate-950 px-2 py-1.5 text-xs text-slate-200 focus:border-sky-700 focus:outline-none"
            >
              {customers.map((c) => (
                <option key={c.customer_id} value={c.customer_id}>
                  {c.name} — {c.city} · kyc {c.kyc_status}
                  {c.do_not_call ? " · DO NOT CALL" : ""}
                </option>
              ))}
            </select>

            <label className="mb-1 block text-[11px] text-slate-400">
              Your name
            </label>
            <input
              value={agentName}
              onChange={(e) => setAgentName(e.target.value)}
              className="mb-3 w-full rounded border border-slate-800 bg-slate-950 px-2 py-1.5 text-xs text-slate-200 focus:border-sky-700 focus:outline-none"
            />

            <button
              onClick={startVoiceCall}
              disabled={busy || !voiceCustomer}
              className="w-full rounded bg-emerald-600 px-3 py-2 text-xs font-semibold text-white hover:bg-emerald-500 disabled:opacity-40"
            >
              {busy ? "Opening session…" : "Customer consented — start voice call"}
            </button>
            <button
              onClick={() => setPhase("mode")}
              className="mt-2 w-full text-[11px] text-slate-500 hover:text-slate-300"
            >
              back
            </button>
          </div>
        </div>
      )}

      {/* ---------- call picker ---------- */}
      {phase === "select" && (
        <div className="flex flex-1 items-center justify-center p-6">
          <div className="w-full max-w-2xl">
            <h2 className="mb-1 text-base font-semibold text-slate-200">
              Choose a call to assist
            </h2>
            <p className="mb-4 text-xs text-slate-500">
              Each scenario exercises a different part of the system.
            </p>
            <div className="space-y-2">
              {calls.map((c) => (
                <button
                  key={c.call_id}
                  onClick={() => choose(c.call_id)}
                  className="w-full rounded-lg border border-slate-800 bg-slate-900/60 p-3 text-left transition hover:border-sky-800 hover:bg-slate-900"
                >
                  <div className="flex items-center justify-between">
                    <span className="font-mono text-xs text-slate-300">
                      {c.call_id}
                    </span>
                    <span
                      className={`chip ${
                        c.outcome === "converted"
                          ? "bg-emerald-500/10 text-emerald-300 ring-emerald-500/30"
                          : "bg-amber-500/10 text-amber-300 ring-amber-500/30"
                      }`}
                    >
                      {c.outcome}
                    </span>
                  </div>
                  <p className="mt-1 text-[11px] leading-snug text-slate-500">
                    {c.scenario}
                  </p>
                </button>
              ))}
              {!calls.length && <Empty>Loading calls…</Empty>}
            </div>
          </div>
        </div>
      )}

      {/* ---------- consent gate ---------- */}
      {phase === "consent" && detail && (
        <div className="flex flex-1 items-center justify-center p-6">
          <div className="w-full max-w-lg rounded-lg border border-amber-900/50 bg-slate-900/60 p-5">
            <div className="mb-3 flex items-center gap-2">
              <span className="text-amber-400">⚑</span>
              <h2 className="text-sm font-semibold text-slate-100">
                Mandatory consent disclosure
              </h2>
            </div>
            <p className="mb-3 text-xs leading-relaxed text-slate-400">
              Read this to {detail.crm?.name ?? "the customer"} before anything
              else:
            </p>
            <blockquote className="mb-4 rounded border-l-2 border-amber-600 bg-slate-950 px-3 py-2.5 text-xs italic leading-relaxed text-slate-300">
              “Hi, this is {agentName} calling from PayFlex. Before we start — this
              call may be recorded and I'm using an AI assistant to help me pull up
              accurate information while we talk. Is that alright with you?”
            </blockquote>
            <div className="mb-4 rounded border border-slate-800 bg-slate-950/60 px-3 py-2">
              <p className="text-[11px] leading-snug text-slate-500">
                The orchestrator will not process a single turn until consent is
                recorded. It raises rather than warns — a code-level gate, not a
                UI reminder you can click past.
              </p>
            </div>
            <label className="mb-1 block text-[11px] text-slate-400">
              Your name
            </label>
            <input
              value={agentName}
              onChange={(e) => setAgentName(e.target.value)}
              className="mb-3 w-full rounded border border-slate-800 bg-slate-950 px-2 py-1.5 text-xs text-slate-200 focus:border-sky-700 focus:outline-none"
            />
            <button
              onClick={giveConsent}
              disabled={busy}
              className="w-full rounded bg-emerald-600 px-3 py-2 text-xs font-semibold text-white hover:bg-emerald-500 disabled:opacity-40"
            >
              {busy ? "Opening session…" : "Customer consented — start call"}
            </button>
          </div>
        </div>
      )}

      {/* ---------- live workspace ---------- */}
      {(phase === "live" ||
        phase === "ended" ||
        phase === "review" ||
        phase === "voice_live" ||
        phase === "phone") && (
        <div className="grid flex-1 grid-cols-12 gap-3 overflow-hidden p-3">
          {/* transcript */}
          <div className="col-span-4 flex flex-col overflow-hidden">
            <div className="panel flex min-h-0 flex-1 flex-col">
              <div className="panel-title flex items-center justify-between">
                <span>Live transcript</span>
                {detail?.crm && (
                  <span className="normal-case tracking-normal text-slate-500">
                    {detail.crm.name} · {detail.crm.city}
                  </span>
                )}
              </div>
              <div ref={scrollRef} className="min-h-0 flex-1 space-y-2 overflow-y-auto p-3">
                {turns.map((t) => {
                  const a = assistFor(t.index);
                  const isCustomer = t.speaker === "customer";
                  return (
                    <div
                      key={t.index}
                      className={`rounded px-2.5 py-1.5 text-xs leading-relaxed ${
                        isCustomer
                          ? "bg-slate-800/60 text-slate-200"
                          : "bg-sky-950/40 text-sky-100/80"
                      } ${a?.blocked ? "ring-1 ring-rose-800" : ""}`}
                    >
                      <div className="mb-0.5 flex items-center justify-between">
                        <span className="text-[10px] font-semibold uppercase tracking-wide text-slate-500">
                          {isCustomer ? "customer" : "agent"}
                        </span>
                        {a?.intent && (
                          <span className="chip bg-slate-900 text-slate-400 ring-slate-700">
                            {a.intent.intent}
                          </span>
                        )}
                      </div>
                      {t.text}
                    </div>
                  );
                })}
                {thinking !== null && (
                  <div className="flex items-center gap-2 px-2.5 py-1.5 text-[11px] text-slate-500">
                    <Spinner /> analysing turn {thinking}…
                  </div>
                )}
                {!turns.length && <Empty>Waiting for the first turn…</Empty>}
              </div>

              {(phase === "ended" ||
                phase === "voice_live" ||
                phase === "phone") &&
                !post && (
                  <div className="border-t border-slate-800 p-3">
                    <button
                      onClick={finalise}
                      disabled={busy || !turns.length || !selected}
                      className="w-full rounded bg-sky-600 px-3 py-2 text-xs font-semibold text-white hover:bg-sky-500 disabled:opacity-40"
                    >
                      {busy
                        ? "Summarising…"
                        : phase === "ended"
                          ? "Call ended — generate CRM update"
                          : "End call — generate CRM update"}
                    </button>
                  </div>
                )}
            </div>
          </div>

          {/* assist / review */}
          <div className="col-span-5 space-y-3 overflow-y-auto pr-1">
            {phase === "review" && post ? (
              <PostCallReview result={post} onApproved={() => {}} />
            ) : latest ? (
              <AssistPanel a={latest} />
            ) : (
              <div className="panel">
                <div className="panel-title">Agent assist</div>
                <Empty>
                  Assistance appears here as the customer speaks.
                </Empty>
              </div>
            )}
          </div>

          {/* voice controls + cost */}
          <div className="col-span-3 space-y-3 overflow-y-auto pr-1">
            {phase === "phone" && (
              <PhoneCall
                onAttached={(id) => {
                  setSelected(id);
                  setLiveCallId(id);
                }}
                onTurn={(t) => setTurns((prev) => [...prev, t])}
                onAssist={(a) => {
                  setThinking(null);
                  setAssists((prev) => [...prev, a]);
                }}
                onLedger={(l, f) => {
                  setLedger(l);
                  setFrontierUsd(f);
                }}
              />
            )}
            {phase === "voice_live" && liveCallId && (
              <>
                <LiveVoice
                  callId={liveCallId}
                  onTurn={(t) => setTurns((prev) => [...prev, t])}
                  onAssist={(a) => {
                    setThinking(null);
                    setAssists((prev) => [...prev, a]);
                  }}
                  onLedger={(l, f) => {
                    setLedger(l);
                    setFrontierUsd(f);
                  }}
                  onEnd={() => finalise()}
                />
                <AudioUpload
                  callId={liveCallId}
                  onTurn={(t) => setTurns((prev) => [...prev, t])}
                  onAssist={(a) => setAssists((prev) => [...prev, a])}
                  onLedger={(l, f) => {
                    setLedger(l);
                    setFrontierUsd(f);
                  }}
                />
              </>
            )}
            <CostMeter ledger={ledger} frontierUsd={frontierUsd} />
          </div>
        </div>
      )}
    </div>
  );
}

import type { CallDetail, CallSummary, PostCallResult } from "./types";

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`${res.status} ${body}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  health: () => fetch("/api/health").then(json<{ status: string; mode: string }>),

  policy: () => fetch("/api/policy").then(json<any>),

  listCalls: () => fetch("/api/calls").then(json<CallSummary[]>),

  getCall: (id: string) => fetch(`/api/calls/${id}`).then(json<CallDetail>),

  /** Opens the call session. Every downstream step is gated on this. */
  consent: (id: string, agentName: string) =>
    fetch(`/api/calls/${id}/consent`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ consent_ack: true, agent_name: agentName }),
    }).then(json<{ call_id: string; consent_ack: boolean }>),

  finalise: (id: string) =>
    fetch(`/api/calls/${id}/finalise`, { method: "POST" }).then(json<PostCallResult>),

  approve: (
    id: string,
    body: {
      approver_id: string;
      decision: "approve" | "reject";
      edited_summary?: string;
      edited_followup_body?: string;
    },
  ) =>
    fetch(`/api/calls/${id}/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(json<any>),

  transcribe: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return fetch("/api/transcribe", { method: "POST", body: fd }).then(json<any>);
  },

  /** Open a live microphone call. Consent is captured in the same request. */
  liveStart: (customerId: string, agentName: string) =>
    fetch("/api/live/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        customer_id: customerId,
        agent_name: agentName,
        consent_ack: true,
      }),
    }).then(json<{ call_id: string; customer_id: string; stt_model: string }>),

  /** Upload one clip and run it through the full pipeline. */
  audioTurn: (callId: string, file: File, speaker: "customer" | "agent") => {
    const fd = new FormData();
    fd.append("file", file);
    return fetch(
      `/api/live/${callId}/audio-turn?speaker=${speaker}`,
      { method: "POST", body: fd },
    ).then(json<any>);
  },

  customers: () => fetch("/api/customers").then(json<any[]>),
};

export function openCallSocket(callId: string): WebSocket {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return new WebSocket(`${proto}://${location.host}/ws/call/${callId}`);
}

export const fmtUsd = (n: number) =>
  n >= 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(6)}`;

export const fmtInr = (n: number) => `₹${n.toFixed(4)}`;

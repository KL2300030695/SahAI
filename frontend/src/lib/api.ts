import type { CallDetail, CallSummary, PostCallResult } from "./types";

/**
 * Unwrap a response, turning a failure into a sentence rather than a status.
 *
 * The dashboard used to show "500 Internal Server Error" verbatim when the Groq
 * daily quota ran out mid-call, which tells an agent with a customer on the
 * line nothing about whose fault it is or what to do. The server now sends a
 * plain-English `detail`; this makes sure it survives the trip.
 */
async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.text();
    let message = body;
    try {
      const parsed = JSON.parse(body);
      if (typeof parsed?.detail === "string") message = parsed.detail;
      else if (Array.isArray(parsed?.detail)) {
        // FastAPI validation errors arrive as a list of objects.
        message = parsed.detail
          .map((d: any) => d?.msg ?? JSON.stringify(d))
          .join("; ");
      }
    } catch {
      // Not JSON — an unhandled server error, a proxy page, or an empty body.
      if (!message.trim()) message = res.statusText || "request failed";
    }
    // The status only earns its place when the body says nothing useful.
    const bare = /^internal server error$/i.test(message.trim());
    throw new Error(bare ? `${res.status} ${message}` : message);
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

  /** The record as it stands, so approval can show before → after. */
  customer: (id: string) =>
    fetch(`/api/customers/${id}`).then(json<Record<string, unknown>>),
};

export function openCallSocket(callId: string): WebSocket {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return new WebSocket(`${proto}://${location.host}/ws/call/${callId}`);
}

export const fmtUsd = (n: number) =>
  n >= 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(6)}`;

export const fmtInr = (n: number) => `₹${n.toFixed(4)}`;

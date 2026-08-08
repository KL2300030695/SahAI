import type { CallDetail, CallSummary, PostCallResult } from "./types";

/**
 * The dashboard's credential.
 *
 * Set VITE_API_KEY when the backend runs with AUTH_ENABLED=1. Left unset the
 * header is simply absent, which is what a fresh clone does — reads stay open
 * and writes are refused with a 401 that says which header is missing.
 *
 * A browser cannot set headers on a WebSocket handshake, so socket URLs carry
 * the key as a query parameter instead. That is a real difference in exposure
 * (a URL is likelier to be logged by a proxy), which is why it is the exception
 * rather than the rule.
 */
const KEY_STORAGE = "sahai.apiKey";

/**
 * The credential is held in localStorage, not in memory.
 *
 * The alternative — keeping it in a React state only — logs the agent out on
 * every refresh, and this dashboard gets refreshed a lot mid-development. The
 * tradeoff is honest and worth stating: localStorage is readable by any script
 * running on the page, so an XSS bug becomes a credential leak. That is
 * acceptable for an API key scoped to one internal tool and rotated by an
 * admin; it would not be for a bearer token that reached a payment system.
 *
 * VITE_API_KEY still works and takes precedence, so a deployment can bake in a
 * credential without anyone signing in.
 */
export function getApiKey(): string {
  const baked = (import.meta as any).env?.VITE_API_KEY;
  if (baked) return String(baked);
  try {
    return localStorage.getItem(KEY_STORAGE) ?? "";
  } catch {
    return ""; // private browsing, or storage disabled
  }
}

export function setApiKey(key: string): void {
  try {
    if (key) localStorage.setItem(KEY_STORAGE, key);
    else localStorage.removeItem(KEY_STORAGE);
  } catch {
    /* nothing we can do; the session simply will not persist */
  }
}

export function authHeaders(): Record<string, string> {
  const k = getApiKey();
  return k ? { "X-API-Key": k } : {};
}

/**
 * A browser cannot set headers on a WebSocket handshake, so socket URLs carry
 * the key as a query parameter. That is a real difference in exposure — a URL
 * is likelier to be logged by a proxy than a header — which is why it is the
 * exception rather than the rule.
 */
export function withKey(url: string): string {
  const k = getApiKey();
  return k ? `${url}${url.includes("?") ? "&" : "?"}api_key=${encodeURIComponent(k)}` : url;
}

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
  health: () => fetch("/api/health", { headers: authHeaders() }).then(json<{ status: string; mode: string }>),

  policy: () => fetch("/api/policy", { headers: authHeaders() }).then(json<any>),

  listCalls: () => fetch("/api/calls", { headers: authHeaders() }).then(json<CallSummary[]>),

  getCall: (id: string) => fetch(`/api/calls/${id}`, { headers: authHeaders() }).then(json<CallDetail>),

  /** Opens the call session. Every downstream step is gated on this. */
  consent: (id: string, agentName: string) =>
    fetch(`/api/calls/${id}/consent`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ consent_ack: true, agent_name: agentName }),
    }).then(json<{ call_id: string; consent_ack: boolean }>),

  finalise: (id: string) =>
    fetch(`/api/calls/${id}/finalise`, { method: "POST", headers: authHeaders() }).then(json<PostCallResult>),

  approve: (
    id: string,
    body: {
      // No approver_id: the server takes the identity from the credential.
      decision: "approve" | "reject";
      send_to_customer?: boolean;
      edited_summary?: string;
      edited_followup_body?: string;
    },
  ) =>
    fetch(`/api/calls/${id}/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify(body),
    }).then(json<any>),

  transcribe: (file: File) => {
    const fd = new FormData();
    fd.append("file", file);
    return fetch("/api/transcribe", { method: "POST", body: fd, headers: authHeaders() }).then(json<any>);
  },

  /** Open a live microphone call. Consent is captured in the same request. */
  liveStart: (customerId: string, agentName: string) =>
    fetch("/api/live/start", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
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
      { method: "POST", body: fd, headers: authHeaders() },
    ).then(json<any>);
  },

  /** Where an approved follow-up would actually be delivered. */
  deliveryPreview: (id: string) =>
    fetch(`/api/calls/${id}/delivery-preview`, { headers: authHeaders() }).then(
      json<{
        configured: boolean;
        customer_email: string;
        redirect_active: boolean;
        goes_to_by_default: string;
        goes_to_if_direct: string;
        direct_possible: boolean;
        direct_blocked_reason: string;
      }>,
    ),

  /** Who the credential says we are, and what it may do. */
  me: () =>
    fetch("/api/me", { headers: authHeaders() }).then(
      json<{
        name: string;
        role: string;
        authenticated: boolean;
        auth_enabled: boolean;
        can: Record<string, boolean>;
      }>,
    ),

  customers: () => fetch("/api/customers", { headers: authHeaders() }).then(json<any[]>),

  /** The record as it stands, so approval can show before → after. */
  customer: (id: string) =>
    fetch(`/api/customers/${id}`, { headers: authHeaders() }).then(json<Record<string, unknown>>),
};

export function openCallSocket(callId: string): WebSocket {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return new WebSocket(withKey(`${proto}://${location.host}/ws/call/${callId}`));
}

export const fmtUsd = (n: number) =>
  n >= 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(6)}`;

export const fmtInr = (n: number) => `₹${n.toFixed(4)}`;

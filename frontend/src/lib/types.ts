// Mirrors backend/app/schemas.py. Kept deliberately close to the Python
// contracts so a change on either side is obvious in review.

export type Tier = "none" | "tiny" | "cheap" | "standard" | "high" | "safety" | "stt";

export interface TranscriptTurn {
  index: number;
  speaker: "agent" | "customer";
  text: string;
  ts: number;
}

export interface IntentOut {
  intent: string;
  confidence: number;
  entities: Record<string, string>;
  dropoff_risk: number;
  escalate: boolean;
  rationale: string;
}

export interface Citation {
  doc_id: string;
  title: string;
  chunk_id: string;
  text: string;
  score: number;
  version: string;
  effective_from: string | null;
  effective_to: string | null;
  source_path: string | null;
}

export interface RetrievalOut {
  query: string;
  citations: Citation[];
  dropped_stale: string[];
}

export interface NBAOut {
  say: string;
  why: string;
  action_type: string;
  cited_chunk_ids: string[];
  requires_human_confirmation: boolean;
}

export interface CheckResult {
  name: string;
  passed: boolean;
  detail: string;
  enforced_by: "code" | "llm";
  severity: "info" | "warn" | "block";
}

export interface CheckOut {
  passed: boolean;
  checks: CheckResult[];
  redacted_say: string | null;
  blocked_reason: string | null;
}

export interface TurnAssist {
  call_id: string;
  turn: TranscriptTurn;
  intent: IntentOut | null;
  retrieval: RetrievalOut | null;
  nba: NBAOut | null;
  guardrail: CheckOut | null;
  blocked: boolean;
  tier_path: string[];
  cost_usd: number;
  latency_ms: number;
}

export interface DecisionCost {
  call_id: string;
  turn_index: number | null;
  agent: string;
  tier: Tier;
  model: string;
  prompt_tokens: number;
  completion_tokens: number;
  usd: number;
  latency_ms: number;
  escalation_trigger: string | null;
}

export interface CostLedger {
  call_id: string;
  decisions: DecisionCost[];
  total_usd: number;
  total_inr: number;
  by_tier_usd: Record<string, number>;
  llm_calls: number;
  zero_cost_steps: number;
}

export interface FollowUpDraft {
  channel: "email" | "sms";
  subject: string;
  body: string;
}

export interface CRMOut {
  summary: string;
  crm_patch: Record<string, unknown>;
  disposition: string;
  dropoff_reason: string | null;
  followup_draft: FollowUpDraft | null;
  send_status: string;
}

export interface PostCallResult {
  call_id: string;
  crm: CRMOut;
  guardrail: CheckOut;
  ledger: CostLedger;
  frontier_usd?: number;
}

export interface CallSummary {
  call_id: string;
  customer_id: string;
  agent_name: string;
  outcome: string;
  scenario: string;
  turns: number;
}

export interface CallDetail extends Omit<CallSummary, "turns"> {
  turn_count: number;
  crm: {
    customer_id: string;
    name: string;
    city: string;
    kyc_status: string;
    credit_limit_inr: number | null;
    past_interactions: string[];
    last_disposition: string | null;
  } | null;
  consent_ack: boolean;
  mode: "mock" | "live";
}

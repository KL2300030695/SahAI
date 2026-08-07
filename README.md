# SahAI — AI Voice Co-Pilot for Inside Sales

Real-time assistance for sales agents selling a **pay-in-3, zero-cost EMI**
product: understands customer intent mid-call, surfaces grounded product terms,
suggests the next best action, and closes the loop with a CRM update and a
follow-up draft — behind a human approval gate.

Six specialised agents, five open-weights models, seven guardrails.

> **Measured: $0.0032 per assisted call (₹0.27) — a 57× cost reduction** against
> the same token volume through a single frontier-model mega-prompt. That figure
> comes from the `usage` field of real API responses, priced by
> `backend/app/config.py`. Nothing in it is estimated.

---

## Quick start

Prerequisites: **Python 3.10+**, **Node 18+**, and a [Groq API key](https://console.groq.com/keys).

```bash
# 1. Configure
cp .env.example .env          # then put your GROQ_API_KEY in .env

# 2. Backend
cd backend
python -m venv .venv
.venv\Scripts\activate         # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt

python -m app.seed.seed_db     # seed the mock CRM
python -m app.rag.ingest       # build the KB index (downloads ~80MB MiniLM once)

uvicorn app.main:app --port 8000 --reload
```

```bash
# 3. Frontend (new terminal)
cd frontend
npm install
npm run dev                    # http://localhost:5173
```

### See it work without the UI

```bash
cd backend
python run_call.py call-001    # sceptical customer, converts
python run_call.py call-004    # prompt-injection attempt + opt-out
python run_call.py call-002 --quiet   # ledger only
```

This prints the full pipeline per turn — intent, citations, suggestion, all
seven guardrail checks, the tier path, and a per-decision cost ledger.

### No API key?

Set `SAHAI_MOCK=1` in `.env`. The demo runs on canned agent outputs through the
**real** orchestrator and the **real** code guardrails. It exists so a live demo
survives a rate limit or dead conference wifi.

---

## The four demo scenarios

| Call | Outcome | What it exercises |
|---|---|---|
| `call-001` | converted | Hidden-charge scepticism. Customer quotes an **outdated ₹199 fee** — the expired 2024 pricing doc ranks highly and is **dropped by the staleness filter**. Also contains a spoken phone number for PII redaction. |
| `call-002` | dropped | Aadhaar-step drop-off. Customer fishes for a limit prediction; the honest answer is *"I can't tell you that"*. Produces a specific, loggable drop-off reason → targeted follow-up. |
| `call-003` | converted | Credit-score objection from a customer with a pending home loan. Two-sided honest answer; soft-vs-hard enquiry distinction. |
| `call-004` | not_interested | **Prompt-injection attempt** halted by the 86M guard, then an explicit opt-out that suppresses the follow-up entirely. |

---

## What the guardrails actually do

Seven checks. **Five are deterministic Python**, not prompt instructions — the
dashboard labels each `code` or `llm` so you can see which survive an
adversarial customer.

- **Consent** — the orchestrator *raises* rather than warns. There is no code
  path that processes a turn without consent on record.
- **Grounding** — every figure in a suggestion must appear in a cited KB chunk.
  Set membership, not judgement. In testing this correctly blocked a suggestion
  claiming KYC takes "5 minutes" when the KB says "under 4".
- **Staleness** — chunks outside their validity window are dropped at retrieval,
  before the model sees them.
- **Human oversight** — `SendStatus` is a state machine. The CRM agent can only
  ever produce `pending_agent_approval`; only `POST /api/calls/{id}/approve`
  with a **named approver** advances it. No agent can reach that endpoint.
- **Opt-out** — an explicit opt-out or a `do_not_call` flag means no follow-up
  is drafted at all. TRAI compliance in code, not model discretion.
- **PII** — Aadhaar, PAN, card, phone, OTP, and email are masked in every log,
  WebSocket frame, and CRM write.

Verified end-to-end:

```
[gate]  WS before consent       → blocked / consent_not_recorded
[turn 5] injection attempt      → halted at tier `tiny`, $0.000001, no reasoning model invoked
[post]  disposition             → not_interested, follow-up SUPPRESSED
[gate]  empty approver          → HTTP 400
[gate]  named approver          → applied, do_not_call=True written to CRM
```

---

## Cost model

| Tier | Model | $/Mtok in→out | Used for |
|---|---|---|---|
| `NONE` | local MiniLM + BM25 + regex | **$0.00** | Retrieval, PII, 5 of 7 guardrails |
| `TINY` | `llama-prompt-guard-2-86m` | 0.035 | Injection screen, every turn |
| `CHEAP` | `llama-3.1-8b-instant` | 0.05 → 0.08 | Intent, entities, drop-off |
| `STANDARD` | `openai/gpt-oss-20b` | 0.075 → 0.30 | Routine suggestions, summary |
| `HIGH` | `openai/gpt-oss-120b` | 0.15 → 0.60 | Credit terms, objections |
| `SAFETY` | `openai/gpt-oss-safeguard-20b` | 0.075 → 0.30 | Policy adjudication |
| `STT` | `whisper-large-v3-turbo` | $0.04/**hour** | Optional audio ingest |

Escalation is a **rule in code** (`app/llm/router.py`) with a named trigger
logged against every decision. `GET /api/policy` returns the whole policy so the
cost claims are inspectable rather than asserted.

---

## Audio

Scripted transcript playback is the primary path — deterministic, and no
live-demo failure mode. **Real audio also works**: `POST /api/transcribe` runs
Whisper on the same Groq key at ~$0.003 for a five-minute call.

Live mic streaming was deliberately not built; see `ARCHITECTURE.md` §8.

---

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Status and mode (live / mock) |
| `GET` | `/api/policy` | Tiers, pricing, escalation rules, business goals |
| `GET` | `/api/calls` | Available demo calls |
| `POST` | `/api/calls/{id}/consent` | **Opens the session. Nothing works before this.** |
| `WS` | `/ws/call/{id}` | Live agent-assist stream |
| `POST` | `/api/calls/{id}/finalise` | Summary, CRM patch, follow-up draft |
| `POST` | `/api/calls/{id}/approve` | **The human gate.** Requires `approver_id`. |
| `GET` | `/api/calls/{id}/ledger` | Per-decision cost ledger |
| `POST` | `/api/transcribe` | Whisper transcription of uploaded audio |

Interactive docs at `http://localhost:8000/docs`.

---

## Tests

```bash
cd backend
.venv\Scripts\python -m pytest -q
```

Covers the deterministic guardrails — grounding, PII redaction, staleness,
credit-term forcing, opt-out detection, and the routing rules.

---

## Documents

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — diagram, agent contracts, cost model, and the two routing bugs found by running it
- [`PITCH.md`](PITCH.md) — problem, solution, unit economics, roadmap
- [`DEMO_SCRIPT.md`](DEMO_SCRIPT.md) — 8-minute walkthrough

---

## Security note

`.env` is gitignored and must never be committed. The key used during
development should be rotated before any public deployment.

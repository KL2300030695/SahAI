# SahAI — AI Voice Co-Pilot for Inside Sales

Real-time assistance for agents selling a **Pay-in-3, zero-cost EMI** product.
SahAI listens to a live call, works out what the customer is asking, looks the
answer up in the product handbook, and puts one sentence on screen for the agent
to say — with every figure in it traced back to the clause it came from.

It never speaks to the customer, and it cannot write to a customer record on its
own. Both of those are enforced in code, not asked for in a prompt.

**Six agents · five open-weights models · eight guardrails · $0.002 per call.**

---

## Table of contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [The AI workflow, stage by stage](#the-ai-workflow-stage-by-stage)
- [Where automation stops](#where-automation-stops)
- [Cost](#cost)
- [Stack](#stack)
- [Setup](#setup)
- [Try it](#try-it)
- [Design decisions worth defending](#design-decisions-worth-defending)
- [Testing](#testing)
- [Known limits](#known-limits)

---

## What it does

An inside-sales agent is on a call. A customer asks *"is there any hidden
charge?"*. In roughly two seconds SahAI has:

1. transcribed the utterance,
2. classified it as `objection_cost` with a sentiment and a drop-off risk,
3. retrieved the two handbook clauses that answer it,
4. routed the turn to a stronger model *because* it touches fees,
5. drafted the sentence to say,
6. run eight checks over that sentence — six of them plain Python,
7. and marked every figure in it that it could trace to a source.

The agent reads one line. Everything else on screen exists to answer *"can I
trust that line?"*

When the call ends, SahAI writes the summary, proposes a CRM patch, drafts a
follow-up for a drop-off — and then stops and waits for a human.

---

## Architecture

```
                        BROWSER (React 18 + Vite)
        ┌──────────────────────────────────────────────────────┐
        │  Say Line — one sentence, traced figures underlined   │
        │  Conversation · Sources · Evidence strip · Cost       │
        └───────▲──────────────────────────┬───────────────────┘
                │ WebSocket (assist push)  │ mic audio / control
                │                          ▼
  ┌─────────────┴──────────────────────────────────────────────┐
  │                    FastAPI  (app/main.py)                   │
  │  /ws/live  ·  /ws/call  ·  /ws/telephony  ·  /ws/observe    │
  │  consent gate · approval gate · CSV export                  │
  └─────────────┬──────────────────────────────────────────────┘
                │
  ┌─────────────▼──────────────────────────────────────────────┐
  │            Orchestrator  (app/orchestrator.py)              │
  │  the only module that knows the pipeline shape              │
  └──┬────────┬─────────┬──────────┬──────────┬────────┬───────┘
     │        │         │          │          │        │
  ┌──▼──┐ ┌──▼───┐ ┌───▼────┐ ┌───▼────┐ ┌───▼───┐ ┌──▼─────┐
  │scrn │ │intent│ │retrieval│ │  NBA   │ │ self  │ │  CRM   │
  │TINY │ │CHEAP │ │  NONE   │ │STD→HIGH│ │ check │ │followup│
  └──┬──┘ └──┬───┘ └───┬────┘ └───┬────┘ └───┬───┘ └──┬─────┘
     │       │         │           │          │        │
     └───────┴─────────┼───────────┴──────────┴────────┘
                       │
        ┌──────────────┼──────────────┬──────────────────┐
        ▼              ▼              ▼                  ▼
   ┌─────────┐   ┌──────────┐   ┌──────────┐      ┌───────────┐
   │  Groq   │   │ Chroma + │   │  SQLite  │      │  Twilio   │
   │ 5 models│   │  BM25    │   │   CRM    │      │  (opt.)   │
   │         │   │ 83 chunks│   │ + ledger │      │ media WS  │
   └─────────┘   └──────────┘   └──────────┘      └───────────┘
```

Agents import from `app/schemas.py` and **never from each other**. That single
constraint is what makes this a multi-agent system rather than a mega-prompt
with headings: each agent can be tested, swapped, or re-tiered on its own,
because its only coupling is to a Pydantic type.

```
backend/app/
├── schemas.py         every agent contract — read this file first
├── orchestrator.py    wires the agents, applies the gates between them
├── agents/            injection · intent · nba · selfcheck · crm
├── guardrails/        rules.py (deterministic checks) · pii.py
├── llm/               client.py (Groq + failure translation) · router.py
├── rag/               ingest.py · retriever.py (hybrid + confidence floor)
├── crm/               models.py · db.py (the only writer of customer rows)
├── telemetry/         cost.py — per-decision ledger
├── telephony/         twilio.py · audio.py (pure-Python G.711)
└── export.py          calls.csv · trace.csv
```

---

## The AI workflow, stage by stage

Every stage has a typed input and output. Cost and tier are recorded for each.

| # | Stage | Tier | Model | What it does |
|---|-------|------|-------|--------------|
| 0 | **Consent gate** | — | code | Raises unless consent is on record. No code path processes a turn without it. |
| 1 | **Transcribe** | STT | `whisper-large-v3-turbo` | Per-utterance, segmented on silence by browser VAD. |
| 2 | **Injection screen** | TINY | `llama-prompt-guard-2-86m` | 86M-param classifier. An attack never reaches a reasoning model. |
| 3 | **Intent** | CHEAP | `llama-3.1-8b-instant` | `{intent, confidence, entities, sentiment, dropoff_risk, buying_signals}` |
| 4 | **Retrieval** | NONE | local | Chroma (ONNX MiniLM) + BM25, fused by RRF. **$0.00**, single-digit ms. |
| 5 | **Next best action** | STANDARD→HIGH | `gpt-oss-20b` / `120b` | One sentence to say, plus the chunk ids it used. |
| 6 | **Self-check** | code → SAFETY/HIGH | `gpt-oss-safeguard-20b` | 8 checks, 6 of them pure Python. |
| 7 | **Post-call** | HIGH | `gpt-oss-120b` | Summary, disposition, CRM patch, follow-up draft. |
| 8 | **Approval** | — | **human** | The only path that writes to a customer record. |

### Routing is code, not prompt text

Six named predicates decide whether a turn goes to the 20B or the 120B. They
live in [`llm/router.py`](backend/app/llm/router.py) and are surfaced at
`/api/policy` so the tiering is inspectable rather than a claim on a slide:

```
sensitive_intent          intent touches eligibility, cost, trust, complaint
credit_terms_in_context   the customer's own words mention credit terms
high_dropoff_risk         risk > 0.6
low_intent_confidence     classifier confidence < 0.55
negative_sentiment        frustrated, angry, or in a hurry
agent_requested           the agent asked for a second opinion
```

> One of these rules used to read the *retrieved KB text* as well as the
> customer's words. That sounds reasonable and is badly wrong: this is a credit
> product, so every retrieved chunk mentions fees, and ~90% of turns escalated.
> Scoping it to the customer's own words took cost from $0.0043 to $0.0032.

### Retrieval refuses to guess

Reciprocal rank fusion scores *rank position*, not relevance — so before the
confidence floor existed, the top hit for *"how do I reset my wifi router"*
scored exactly as highly as the top hit for *"what is the late fee"*, and four
confident-looking chunks with real ids came back for both.

The floor was measured, not guessed
([`scripts/calibrate_retrieval.py`](backend/scripts/calibrate_retrieval.py)):

| on the intent-expanded query | min | max |
|---|---|---|
| in-domain (20 utterances) | **0.4807** | 0.7008 |
| off-topic (12 utterances) | 0.1406 | **0.4480** |

`MIN_COSINE = 0.40`. Below it, **no citations are returned at all** — not a
low-confidence flag beside the chunks. A model handed plausible fee tables will
quote them whatever flag sits alongside; withholding the text is the only thing
that works. BM25 stays a *ranking* contributor (it is what catches "₹250" and
"PAN") and is never consulted for confidence, because measured on this corpus
*"how do I reset my wifi router"* outscores most genuine product questions on
BM25 alone — it rewards the common tokens in "how do I".

### The eight guardrails

`enforced_by` is shipped to the UI so a reviewer can see which are
un-promptable code and which are model judgement.

| Check | By | What it catches |
|---|---|---|
| `consent_recorded` | **code** | Any turn processed without consent |
| `pii_redaction` | **code** | Aadhaar, PAN, card, phone — masked before storage *and* display |
| `grounding` | **code** | Any figure not present verbatim in a cited chunk |
| `no_stale_terms` | **code** | Clauses past their `effective_to` date |
| `no_autonomous_credit_terms` | **code** | Forces human confirmation on credit language |
| `no_fabricated_actions` | **code** | *"I've already sent you an email"* — the worst available failure |
| `injection_screen` | llm | Prompt-injection attempts |
| `goal_alignment` | llm | Drift from the business conduct policy |

---

## Where automation stops

This is the part worth reading closely, because it is enforced by a state
machine and not by a comment.

```
  AUTOMATIC — no human in the loop
  ├─ transcription → intent → retrieval → suggestion → surfaced to agent
  ├─ post-call summary, disposition, drop-off reason
  ├─ CRM patch  ← proposed, written to the call log
  └─ follow-up message  ← drafted

  ══════════════ pending_agent_approval ══════════════

  REQUIRES A NAMED HUMAN
  ├─ applying the patch to the customer record
  └─ sending the message
```

`POST /api/calls/{id}/approve` is the only route that can move that state, it
requires an approver name, and **no agent can call it**.

It also refuses to send a message its own guardrail rejected. That sounds
obvious; it was not true until recently. The post-call check correctly caught a
draft claiming Pay-in-3 was entirely free with no mention of the late fee, wrote
the failing verdict to the trace — and the endpoint never read it. The message
went out marked `sent`. **A guardrail nothing consumes is decoration.**

The way past a block is a rewrite, not another click. An edited message is
re-checked against the deterministic rules, and the override is recorded in the
audit trace under the approver's name — because the accountable act is a human
choosing different words, not a human dismissing a warning.

---

## Cost

**Median $0.0020 per assisted call (₹0.17) — a measured 53× reduction.**

Every figure below comes from the `usage` field of real API responses, priced by
`backend/app/config.py` and persisted per decision. Nothing is estimated. n=20
real calls.

| Seed call | SahAI | Same tokens, one frontier mega-prompt | |
|---|---|---|---|
| `call-001` won / hidden charges | $0.003630 | $0.204900 | **56×** |
| `call-002` dropped / Aadhaar | $0.008490 | $0.398505 | **47×** |
| `call-004` dropped / not interested | $0.003025 | $0.204015 | **67×** |

Where the money goes, across all 20 measured calls:

| Tier | Stages | Tokens | USD | Share |
|---|---:|---:|---:|---:|
| high | 70 | 91,654 | 0.017880 | 43.8% |
| standard | 54 | 85,832 | 0.008455 | 20.7% |
| safety | 48 | 53,427 | 0.005392 | 13.2% |
| stt | 88 | — | 0.005354 | 13.1% |
| cheap | 103 | 68,718 | 0.003644 | 8.9% |
| tiny | 106 | 1,771 | 0.000062 | 0.2% |
| **none** | **194** | — | **0.000000** | **0.0%** |

**194 of 663 stages (29%) cost nothing** — retrieval and six of the eight
guardrails are local compute. That is the concrete form of *"a smaller model or
a classical method can replace constant LLM calls"*.

The baseline is deliberately conservative: it prices only the tokens actually
spent. A real mega-prompt would also carry the whole handbook in context on
every turn instead of retrieving four chunks, so the true gap is wider.

```bash
# Reproduce it yourself
curl "localhost:8000/api/export/trace.csv?call_id=call-001" -o trace.csv
```

---

## Stack

| Layer | Choice | Why this one |
|---|---|---|
| Models | **Groq** — `llama-prompt-guard-2-86m`, `llama-3.1-8b-instant`, `gpt-oss-20b`, `gpt-oss-120b`, `gpt-oss-safeguard-20b`, `whisper-large-v3-turbo` | Open weights, five tiers to route between, fast enough for a live call |
| API | **FastAPI 0.115** + WebSockets | Async, typed, and WS is what a live call needs |
| Contracts | **Pydantic 2.10** | The agent boundary *is* the schema |
| Vectors | **Chroma 0.5** with ONNX MiniLM | Runs locally, **no torch** — keeps the install small |
| Lexical | **rank-bm25** | Embeddings are weak on "₹250" and "PAN"; BM25 is not |
| CRM | **SQLAlchemy 2.0 + SQLite** | Zero-setup, real transactions, survives a restart |
| Frontend | **React 18 + Vite 6 + TypeScript 5.7 + Tailwind 3.4** | Fast HMR; tokens as CSS custom properties |
| Telephony | **Twilio Media Streams** (optional) | Dual-track audio gives exact speaker attribution |
| Tests | **pytest 8.3** — 178, all offline | No network in the test suite |

---

## Setup

**Prerequisites:** Python 3.10+, Node 18+, and a
[Groq API key](https://console.groq.com/keys).

### 1. Backend

```bash
cd backend
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Configure

Create `.env` in the **repo root** (it is gitignored — never commit it):

```ini
GROQ_API_KEY=gsk_your_key_here

MODEL_TINY=meta-llama/llama-prompt-guard-2-86m
MODEL_CHEAP=llama-3.1-8b-instant
MODEL_STANDARD=openai/gpt-oss-20b
MODEL_HIGH=openai/gpt-oss-120b
MODEL_SAFETY=openai/gpt-oss-safeguard-20b
MODEL_STT=whisper-large-v3-turbo

SAHAI_MOCK=0                    # 1 = run with no API calls at all
PLAYBACK_INTERVAL_SECONDS=3.5   # scripted-call pacing
DATABASE_URL=sqlite:///./data/sahai.db
CHROMA_DIR=./app/rag/store
CORS_ORIGINS=http://localhost:5173
USD_TO_INR=83.0
```

### 3. Build the knowledge base and seed the CRM

```bash
python -m app.rag.ingest      # 19 documents → 83 chunks
python -m app.seed.seed_db    # customers, call history
```

### 4. Run

```bash
# terminal 1
uvicorn app.main:app --reload --port 8000

# terminal 2
cd frontend && npm install && npm run dev
```

Open **http://localhost:5173**.

### 5. Optional — a real phone line

```ini
PUBLIC_BASE_URL=https://your-tunnel.ngrok-free.dev
TWILIO_AUTH_TOKEN=your_token
```

Point your Twilio number's voice webhook at `POST {PUBLIC_BASE_URL}/api/telephony/voice`.
Without a number, `python simulate_phone_call.py` (from `backend/`) drives the
same code path end to end, signature verification included.

---

## Try it

### The 90-second demo

1. Pick **call-001 — hidden charges** on the idle screen.
2. Read the consent line. Nothing runs until you do — that is a code gate.
3. Watch the **Say Line**. Figures with a teal underline are traced to a clause;
   hover one for the document, version, and chunk id.
4. On the credit-terms turn, the band turns amber: *you* confirm before saying it.
5. End the call. Read the before → after diff, sign it, send.

### Show the machinery

```bash
# One call through every stage, with the full cost breakdown
python -m scripts.run_full_pipeline call-001

# Reproduce the retrieval confidence floor from measurements
python -m scripts.calibrate_retrieval

# The routing policy and pricing, as served
curl localhost:8000/api/policy | python -m json.tool
```

### Export

```
GET /api/export/calls.csv[?call_id=…]    one row per call
GET /api/export/trace.csv[?call_id=…]    one row per pipeline stage
```

Both open in Google Sheets by dragging the file in. The automation boundary is
legible in the columns: everything up to `followup_body` is machine-written;
`send_status`, `approved_by` and `approved_at` are the only fields no agent can
set.

### No API key?

```ini
SAHAI_MOCK=1
```

Every path runs, nothing is billed, and the cost ledger reads zero.

---

## Design decisions worth defending

**The AI never speaks to the customer.** A human reads the line and says it in
their own words. That is what makes the guardrails meaningful — there is always
a person between the model and the customer.

**Guardrails are code where they can be.** Six of eight checks are plain Python
over the generated text. They cannot be prompt-injected away because they are
not prompts. The two that need judgement are labelled `llm` in the UI, honestly.

**Grounding and the UI share one function.** `ground_figures()` returns the
character offsets that the Say Line underlines *and* the verdict the guardrail
blocks on. One function, one source of truth — the marks and the decision can
never disagree.

**The microphone goes deaf while the co-pilot speaks.** On a laptop the
suggestion comes out of the speakers next to the mic; without a gate the
pipeline transcribes its own voice, files it as the customer, and answers
itself. Nothing in the transcript looks wrong — it just fills with fluent
sentences nobody said.

**Speaker attribution is honest about its limits.** On the phone path the
carrier sends each leg separately, so attribution is exact. On the browser mic
everything is treated as the customer, and the UI says so rather than pretending.

**PII order matters.** A 16-digit card number's first 12 digits match the
Aadhaar pattern. Redacting Aadhaar first produced `card [AADHAAR_REDACTED] 1111`
— leaking four digits. Card is checked first now, and there is a test.

---

## Testing

```bash
cd backend && pytest          # 178 tests, no network
```

Written against the failures that actually happened, not for coverage:

| File | Covers |
|---|---|
| `test_guardrails.py` | The eight checks, PII ordering, sentence trimming |
| `test_approval_gate.py` | A blocked message cannot be sent; a rewrite can |
| `test_retrieval_gate.py` | Off-topic returns *no* source; BM25 is not a confidence signal |
| `test_llm_unavailable.py` | A quota failure reads as a sentence, not a 500 |
| `test_export.py` | The automation boundary is visible in the CSV |
| `test_telephony.py` | G.711 codec, signature verification, VAD segmentation |

---

## Known limits

Stated rather than hidden.

- **Grounding validates only numeric claims.** *"We never share your Aadhaar"*
  contains no figures and passes unchecked. The `goal_alignment` LLM check is
  the only net under non-numeric assertions. This is the largest open gap.
- **Live-call state is in memory.** A backend restart loses in-flight calls.
  Fine for one process; a real deployment needs Redis.
- **Browser-mic calls cannot do two-party attribution.** Single-microphone
  capture physically cannot separate speakers. Use the phone path for that.
- **Groq's daily token cap is per *organization*, not per key.** Roughly seven
  full calls exhaust a free-tier day, and issuing a new API key on the same
  account does not reset it.
- **Speaker diarization is not implemented.** The phone path gets attribution
  from the carrier's separate tracks instead.

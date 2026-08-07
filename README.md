# SahAI — AI Voice Co-Pilot for Inside Sales

Real-time assistance for agents selling a **Pay-in-3, zero-cost EMI** product.
SahAI listens to a live call, works out what the customer is asking, looks the
answer up in the product handbook, and puts one sentence on screen for the agent
to say — with every figure in it traced back to the clause it came from.

It never speaks to the customer, and it cannot write to a customer record on its
own. Both of those are enforced in code, not asked for in a prompt.

**Six agents · five open-weights models · eight guardrails · $0.0038 per call.**

---

> **Presenting or reviewing this?** Read
> **[`docs/SahAI-Project-Brief.pdf`](docs/SahAI-Project-Brief.pdf)** — a 12-page
> brief covering how the whole system works, every measured number, the known
> limits, the bugs we found, and an 8-minute demo script. It is written to be
> read cold by someone who has never seen the code.

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
  │  /ws/live  ·  /ws/call  ·  CSV + Firestore + Sheets export  │
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
   │  Groq   │   │ Chroma + │   │  SQLite  │      │ Firestore │
   │ 5 models│   │  BM25    │   │   CRM    │      │  + Sheets │
   │         │   │ 83 chunks│   │ + ledger │      │  (opt.)   │
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

**A full call costs $0.0038 (₹0.31) — a measured 55× reduction.**

Every figure below comes from the `usage` field of real API responses, priced by
`backend/app/config.py` and persisted per decision. Nothing is estimated.

| Seed scenario | Stages | Tokens | SahAI | Same tokens, one frontier mega-prompt | |
|---|---:|---:|---:|---:|---:|
| `call-001` won / hidden charges | 47 | 33,615 | $0.004306 | $0.242400 | **56×** |
| `call-002` dropped / Aadhaar | 48 | 31,527 | $0.004592 | $0.219100 | **48×** |
| `call-003` won / credit score | 54 | 34,359 | $0.004270 | $0.242300 | **57×** |
| `call-004` dropped / not interested | 31 | 16,101 | $0.001860 | $0.112600 | **61×** |

The four scenarios average **$0.0038 (₹0.31)** at **55×**. Across 24 measured
calls including shorter live-mic sessions the median is **53×**. The seed scenarios above are full-length
calls and sit at the expensive end, which is the honest number to quote.

Where the money goes, across all 22:

| Tier | Stages | Tokens | USD | Share |
|---|---:|---:|---:|---:|
| high | 59 | 90,365 | 0.017329 | 42.2% |
| standard | 53 | 89,297 | 0.008760 | 21.3% |
| stt | 92 | — | 0.005653 | 13.8% |
| safety | 48 | 54,122 | 0.005566 | 13.5% |
| cheap | 87 | 70,179 | 0.003717 | 9.0% |
| tiny | 88 | 1,551 | 0.000054 | 0.1% |
| **none** | **169** | — | **0.000000** | **0.0%** |

**180 of 633 stages (28%) cost nothing** — retrieval and six of the eight
guardrails are local compute. That is the concrete form of *"a smaller model or
a classical method can replace constant LLM calls"*.

The baseline is deliberately conservative: it prices only the tokens actually
spent. A real mega-prompt would also carry the whole handbook in context on
every turn instead of retrieving four chunks, so the true gap is wider.

```bash
# Reproduce any row yourself
python -m scripts.run_full_pipeline call-001
curl "localhost:8000/api/export/trace.csv?call_id=call-001" -o trace.csv
```

Re-running a seed scenario **replaces** its ledger rows rather than appending,
so an exported trace always describes one run. Live calls get a fresh id and
never collide.

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

### 5. Optional — Firestore and Google Sheets

Off by default. Unset, SahAI writes to SQLite and CSV and behaves identically —
which is what happens when someone clones this repo, so that path is tested.

Turned on, every finalised call and every approval is mirrored to Firestore and
upserted into a Google Sheet. **SQLite stays the system of record**: `get_crm_snapshot`
runs on every turn of a live call, and a 50–200 ms round trip there would spend a
fifth of the latency budget on network and make the demo depend on the wifi. All
the data reaches Firestore; the customer on the phone never waits for it.

Both mirrors are best-effort. A Firestore outage or a revoked Sheets token is
counted and reported at `/api/integrations/status`, never raised — an agent
mid-conversation should not lose a call because a spreadsheet is unreachable.

**One service account serves both.** In the [Firebase console](https://console.firebase.google.com):

1. **Project settings → Service accounts → Generate new private key** → downloads a JSON file.
   (This is *not* the `firebaseConfig` snippet shown under "Your apps" — that one
   is public client config for the browser and cannot authenticate a server.)
2. Save it to `secrets/firebase-service-account.json` — already gitignored.
3. **Build → Firestore Database → Create database** if you have not yet.
4. For Sheets: enable the **Google Sheets API** in the linked Google Cloud
   project, create a spreadsheet, and share it with the service account's
   `client_email` (it is inside the JSON) as an **Editor**.

```ini
GOOGLE_CREDENTIALS_PATH=./secrets/firebase-service-account.json
FIRESTORE_ENABLED=1
FIRESTORE_PROJECT=your-project-id
FIRESTORE_PREFIX=sahai          # so several people can share one project
SHEETS_ENABLED=1
SHEETS_ID=                      # the id from the sheet URL: /spreadsheets/d/<THIS>/edit
```

Check it and backfill anything recorded before you switched it on:

```bash
curl localhost:8000/api/integrations/status         # what is configured and reachable
curl -X POST localhost:8000/api/integrations/sync   # push every stored call
```

Firestore layout, and the two Sheets tabs, carry the **same rows** the CSV export
produces — one row builder feeds all three, so a file, a document and a
spreadsheet row can never disagree about what a call cost:

```
sahai_calls/{call_id}                one document per call   →  "Calls" tab
sahai_calls/{call_id}/stages/{0000}  the pipeline trace      →  "Trace" tab
sahai_customers/{customer_id}        CRM record as written
```

Replaying a scenario **overwrites** its row and its stages rather than appending,
matching the local ledger. A demo rehearsed three times shows one row, not three
contradictory ones.


---

## Deploying it, and plugging it into your stack

### One command

```bash
docker compose up --build      # → http://localhost:8000
```

One service, not three: the dashboard is built and served by the same process
that serves the API, so there is no proxy to configure and no CORS to get wrong
between a laptop and a server. The knowledge base is indexed at image build
time — otherwise the first call of a demo pays to embed 19 documents and a cold
container looks broken rather than slow.

| | |
|---|---|
| `GET /healthz` | liveness — checks nothing else, so a database blip cannot restart a healthy container into a crash loop |
| `GET /readyz` | readiness — database + knowledge-base index. **Not** the model provider: a quota failure is already a per-request 503 with an explanation, and pulling the instance would take the dashboard down too |
| every response | `X-Request-ID` and `X-Response-Time-ms` — a call fans out across six agents and two mirrors, and timestamps stop correlating the moment two agents are on the phone at once |

Credentials are mounted read-only and excluded from the build context, so the
image can be shared without shipping secrets.

### Access control, and who signs an approval

`POST /approve` is the only route that writes an AI-proposed patch onto a
customer record. Its docstring said no agent could call it. That was true only
in the sense that none *did* — the approver's name arrived as a string in the
request body, so anyone who could reach the port could approve as anyone,
including a script typing a colleague's name.

**The approver is now who the credential says they are, and the body no longer
carries a name at all.** The dashboard shows the identity it will sign with
instead of asking for one, because a field the server ignores teaches the wrong
thing about where the authority comes from.

```ini
AUTH_ENABLED=1
API_KEYS=k_live_abc:Priya Nair:agent,k_live_xyz:Ravi Menon:admin
```

| Role | read | run calls | approve | sync integrations |
|---|:--:|:--:|:--:|:--:|
| `viewer` | ✅ | | | |
| `agent` | ✅ | ✅ | ✅ | |
| `admin` | ✅ | ✅ | ✅ | ✅ |

Keys are compared with `hmac.compare_digest`, reads stay open so a fresh clone
still shows a working dashboard, and writes made while auth is off are stamped
`(unauthenticated)` in the audit trail — otherwise a year later nobody could
tell a signed approval from one made on an open port.

### Swapping in a real CRM

The pipeline talks to a four-method port, not to SQLite:

```
read_snapshot · is_do_not_call · apply_patch · describe
```

`is_do_not_call` is deliberately *not* folded into the snapshot even though both
read the same record. A snapshot is advisory context for a language model;
do-not-call is a legal obligation checked in code before drafting. Merging them
would make the obligation depend on a field surviving a model's attention.

Two adapters ship. `SqliteCrm` is the demo default. `RestCrm` talks to any HTTP
CRM and is **configured, not coded** — including the field names, because
`kyc_status` here is `KYC_Status__c` there:

```ini
CRM_BACKEND=rest
CRM_BASE_URL=https://crm.internal/api/v1
CRM_TOKEN=...
CRM_FIELD_MAP={"kyc_status":"KYC_Status__c"}
```

Its failure policies differ by method on purpose, and each has a test: a failed
**read** degrades to less context and the call continues; a failed
**do-not-call** check returns `True`, because an unreachable CRM is not
permission to call someone who may have opted out; a failed **write** reports
itself and leaves the local state pending rather than losing it silently.

`RestCrm` is tested against a real stub HTTP server rather than a mock, so the
claim is "this works against an HTTP CRM", not "we imagined one".

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

**Speaker attribution is honest about its limits.** Everything the microphone
hears is attributed to the customer, and the interface says so rather than
pretending otherwise. One microphone cannot separate two people in a room; a
carrier-side integration could, by taking each leg of the call on its own track,
but that needs a paid phone number and is out of scope here.

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

---

## Known limits

Stated rather than hidden.

- **Grounding validates only numeric claims.** *"We never share your Aadhaar"*
  contains no figures and passes unchecked. The `goal_alignment` LLM check is
  the only net under non-numeric assertions. This is the largest open gap.
- **Live-call state is in memory.** A backend restart loses in-flight calls.
  Fine for one process; a real deployment needs Redis.
- **Two-party speaker attribution is not possible.** A single microphone cannot
  separate two speakers, so every utterance it hears is attributed to the
  customer. This assumes a speakerphone or a solo run. Real diarization needs
  either a carrier integration (each leg on its own track) or a diarizing STT
  model; both were out of scope.
- **Groq's daily token cap is per *organization*, not per key.** Roughly seven
  full calls exhaust a free-tier day, and issuing a new API key on the same
  account does not reset it.
- **Telephony was removed deliberately.** An earlier version accepted real
  inbound calls over Twilio Media Streams and got exact speaker attribution from
  the carrier's dual tracks. It was cut because a phone number is a recurring
  cost and the browser-microphone path demonstrates the same pipeline. The
  orchestrator never knew about it — it consumes transcript turns, whatever
  produced them — so restoring it is a new adapter, not a redesign.

---

## License

[MIT](LICENSE) © Subhash Vadaparthi

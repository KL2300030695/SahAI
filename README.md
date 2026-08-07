# SahAI — AI Voice Co-Pilot for Inside Sales

Real-time assistance for sales agents selling a **pay-in-3, zero-cost EMI**
product: understands customer intent mid-call, surfaces grounded product terms,
suggests the next best action, and closes the loop with a CRM update and a
follow-up draft — behind a human approval gate.

Six specialised agents, five open-weights models, eight guardrails.

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

This prints the full pipeline per turn — intent, sentiment, citations,
suggestion, all eight guardrail checks, the tier path, and a per-decision cost
ledger.

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

Eight checks. **Six are deterministic Python**, not prompt instructions — the
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
- **No fabricated actions** — a suggestion claiming something already happened
  ("I've sent you an email", "I've marked you as do-not-call") is blocked. The
  assistant has no side effects, and a customer hanging up believing otherwise
  is worse than a wrong fee.
- **Stop selling on a complaint** — `complaint` and `payment_issue` intents, and
  angry or frustrated sentiment, route to a human instead of a pitch.

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
| `NONE` | local MiniLM + BM25 + regex | **$0.00** | Retrieval, PII, 6 of 8 guardrails |
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

## Voice

Four input paths, all running the identical pipeline and the identical consent
gate:

| Path | How |
|---|---|
| **Real phone call** | Twilio forks call audio to `/ws/telephony/stream` as G.711 mu-law. Each party arrives on a separate track, so speaker attribution is exact. See below. |
| **Live microphone** | Browser captures audio, segments it on ~900ms of silence, streams each complete utterance over `/ws/live/{id}`. |
| **Audio upload** | Drop a recorded clip into `POST /api/live/{id}/audio-turn`. Fallback when a mic isn't available. |
| **Scripted playback** | Replays a seeded transcript. Deterministic, no dependencies — the safe demo path. |

### Phone calls

Try it with no Twilio account, no phone number and no tunnel:

```bash
cd backend
python simulate_phone_call.py           # replays a clip as a real inbound call
```

The simulator converts a WAV to mu-law, chops it into 20ms frames, and speaks
Twilio's Media Streams protocol to the real endpoint. Everything downstream is
the production path — same codec handling, same server-side VAD, same Whisper
call, same orchestrator, same guardrails. Only the frame source differs.

**To take an actual call:**

1. Expose the backend — carriers need a public https URL:
   ```bash
   ngrok http 8000
   ```
2. Put it in `.env` and restart:
   ```
   PUBLIC_BASE_URL=https://your-subdomain.ngrok-free.app
   TWILIO_AUTH_TOKEN=your_token      # enables signature verification
   ```
3. In the Twilio console, set your number's **Voice → A call comes in** webhook to:
   ```
   https://your-subdomain.ngrok-free.app/api/telephony/voice
   ```
4. Open the dashboard, choose **Phone call**, and ring the number.

`GET /api/telephony/config` reports exactly what to paste and whether the server
is ready.

**Two design points worth knowing:**

The TwiML uses `<Start><Stream>`, not `<Connect><Stream>`. `<Connect>` hands the
call *to* the socket and expects audio back — that is how you build a bot. This
is a co-pilot: it forks a copy of the audio while the call proceeds normally
between the customer and the human agent, and it never speaks to the customer.

The consent disclosure is a `<Say>` verb *before* the stream starts, so it is
spoken by the platform before a single audio frame is forked. Same property as
the dashboard's consent gate, enforced one layer earlier — an agent under time
pressure cannot skip it.

> **Indian numbers via Twilio require regulatory documentation** (business
> address proof, and a bundle approval that takes days). For a demo, a US trial
> number works fine and calls it from anywhere; the simulator needs nothing at
> all. Plivo and Exotel expose near-identical stream shapes if you need an
> Indian provider — the parsing is confined to `app/telephony/twilio.py`.

Suggestions can also be **read aloud** to the agent via browser speech
synthesis (free, no added latency, no provider).

### Two things worth knowing

**Utterance segmentation, not fixed chunks.** Only the first chunk of a
MediaRecorder stream carries the webm header — later chunks aren't
independently decodable and Whisper rejects them. So the recorder is stopped and
restarted at each silence boundary, producing complete files that map 1:1 onto
pipeline turns.

**Speaker attribution is stated, not guessed.** One microphone can't separate
agent from customer, and Whisper doesn't diarise. The UI asks who is speaking
rather than inventing an answer — getting it wrong would silently poison intent
detection on every later turn.

### The STT bug that mattered

Whisper transcribed a customer saying *"a one ninety nine processing fee"* as
**"a $1.99 processing fee"** — wrong currency *and* wrong magnitude. That breaks
grounding, which matches figures against retrieved chunk text.

Fixed with domain priming (measured: `$1.99` → `199`) plus a normalisation pass
that rewrites `$` amounts as rupees and joins hyphenated spoken numbers
(`1-99` → `199`). This product quotes no USD anywhere, so a dollar sign in a
transcript is always an artefact.

Neither is a guarantee — speech recognition on numbers is probabilistic. The
actual safety property is downstream: **a mis-transcribed figure cannot be
quoted back to the customer**, because grounding won't match it.

---

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Status and mode (live / mock) |
| `GET` | `/api/policy` | Tiers, pricing, escalation rules, business goals |
| `GET` | `/api/calls` | Available demo calls |
| `GET` | `/api/customers` | Customers a live call can be opened against |
| `POST` | `/api/calls/{id}/consent` | **Opens a scripted session. Nothing works before this.** |
| `POST` | `/api/live/start` | **Opens a live voice session** (captures consent) |
| `WS` | `/ws/call/{id}` | Scripted agent-assist stream |
| `WS` | `/ws/live/{id}` | **Live microphone** — binary audio in, assist out |
| `POST` | `/api/live/{id}/audio-turn` | Upload a clip → STT → full pipeline |
| `GET` | `/api/telephony/config` | What to paste into the carrier console |
| `POST` | `/api/telephony/voice` | **TwiML webhook** — the carrier fetches this on an inbound call |
| `WS` | `/ws/telephony/stream` | **Carrier audio** — Twilio Media Streams |
| `GET` | `/api/telephony/active` | Phone calls currently in progress |
| `WS` | `/ws/observe/{id}` | Read-only view of a call driven by the carrier |
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

# SahAI — Business Pitch

**An AI voice co-pilot that makes every inside-sales agent as accurate as your
best one — at half a US cent per call.**

---

## The problem

A fintech launches a pay-in-3, zero-cost EMI product. It is genuinely good: the
customer repays exactly the cart value in three parts. But it is *hard to sell
honestly*, and that is where the money leaks.

**1. The product is easy to explain badly.**
"Zero-cost" invites disbelief. The honest answer involves a business model
(the merchant pays), two real fees (₹250 late, ₹150 bounce), and a two-sided
credit-bureau story. An agent who oversimplifies gets a complaint later; an
agent who fumbles it loses the sale now.

**2. Terms change; memory doesn't.**
The processing fee went from ₹199 to ₹0 and the late fee from ₹500 to ₹250 in
April 2026. Every agent still carrying the old numbers is quoting a customer
terms that no longer exist — a compliance exposure, not just an error.

**3. Half of all drop-off happens at two specific moments.**
31% at the Aadhaar OTP step, 24% at the auto-debit mandate. Both are
pre-emptable in one sentence — *if* the agent knows the moment is coming.

**4. Follow-up is generic, so it doesn't convert.**
"Complete your application" converts at a fraction of "you paused at the Aadhaar
step — nothing is uploaded, and your progress is saved for 7 days." Capturing
the real reason takes discipline no one has at 6pm on call forty.

---

## The solution

SahAI listens alongside the agent and, on every customer turn:

- **classifies intent** and scores drop-off risk
- **retrieves the current terms** from the knowledge base, with citations
- **suggests what to say next**, in the agent's voice, ready to speak
- **checks its own output** against seven guardrails before the agent sees it

After the call it writes the summary, proposes a CRM update, and drafts a
targeted follow-up — all of which sit at `pending_agent_approval` until a named
human approves them.

### What makes it trustworthy

It is a **co-pilot, not a bot**. It never speaks to the customer. Anything
touching credit terms is flagged for the human to confirm and say themselves —
enforced in code, and the flag can be raised by the system but never lowered by
the model.

**Five of the seven guardrails are deterministic Python, not prompt
instructions.** The dashboard labels each `code` or `llm` so a compliance
reviewer can see exactly which survive an adversarial customer:

| Guardrail | Enforced by |
|---|---|
| Consent recorded before any processing | code — the orchestrator *raises* |
| Every quoted figure traceable to a cited source | code — set membership |
| Expired terms dropped before the model sees them | code — validity windows |
| Credit terms require human confirmation | code — flag can only be raised |
| PII masked in every log, frame, and CRM write | code — regex |
| Opt-out suppresses all follow-up | code — TRAI compliance |
| Output aligns with written business goals | llm — policy-tuned model |

---

## Unit economics

Measured from real API responses across the demo calls — not modelled.

| | Per assisted call |
|---|---|
| **SahAI** | **$0.0032 (₹0.27)** |
| Same tokens, one frontier-model mega-prompt | $0.185 (₹15.35) |
| **Reduction** | **57×** |

At **10,000 calls/month: $32 vs $1,855.** At 100,000: $320 vs $18,550.

### Where the reduction comes from

Three levers, not one:

1. **Cost tiering.** An 86M classifier screens every turn; an 8B model does
   intent; a 20B model handles routine suggestions; the 120B model is reserved
   for credit terms and objections. Escalation is a code rule with a named
   trigger logged against every decision.
2. **RAG instead of inference.** Retrieval is the highest-frequency step in the
   pipeline and costs **$0.00** — local embeddings plus BM25. Five of seven
   guardrails likewise. In a typical call, **17 pipeline steps run at zero
   marginal cost.**
3. **Reasoning-effort control.** The reasoning models bill chain-of-thought as
   output tokens. Measured on an identical prompt: `low` = 150 completion
   tokens, `medium` = 334 — 2.2× the cost for the same answer.

An adversarial customer attempting prompt injection is defeated for
**$0.000001**, because the 86M guard halts the pipeline before any reasoning
model is invoked.

---

## Business impact

The lever is not "AI answers calls". It is **variance**. Your best agent already
quotes the right fee, pre-empts the Aadhaar step, and logs the real drop-off
reason. Your median agent does not, and that gap is the addressable loss.

| Where value lands | Mechanism |
|---|---|
| Higher conversion | Right answer at the objection, drop-off pre-empted at the two steps that account for 55% of abandonment |
| Lower compliance risk | Stale terms structurally cannot be quoted; every figure is traceable to a source |
| Better follow-up yield | Specific stated reason captured per drop-off, instead of "not interested" |
| Faster ramp | A new agent has the KB and the objection playbook in front of them from call one |
| Audit trail | Every decision, guardrail result, model tier, and cost is logged per turn |

Deliberately **not** claimed: a conversion-lift percentage. We have no
production A/B data, and a fabricated number is exactly the kind of unsupported
figure the grounding guardrail exists to block.

---

## What's next

**Near term**
- A/B the co-pilot against unassisted agents to get a real conversion delta
- Live mic streaming (Whisper already works on uploaded audio)
- Redis-backed session state so the socket can be served by any worker
- Push the KB to the live product CMS so terms sync automatically

**Medium term**
- Learn the drop-off risk model from outcomes rather than prompting for it
- Distil the intent classifier to a local model — zero marginal cost per turn
- Per-agent coaching from the guardrail trace: which agents get blocked, and why
- Real CRM connectors (Salesforce, LeadSquared)

**Longer term**
- Multilingual (Hindi, Tamil, Telugu, Marathi) — the largest reach constraint in
  Indian inside sales
- Route escalations to a supervisor console in real time
- Close the loop from approved follow-ups back into the next call's context

---

## The honest summary

Most of this system is not a language model. Retrieval is local. The guardrails
that matter are Python. The expensive model is invoked on roughly one turn in
three, and only when a code rule says the turn earns it.

That is the whole design: **spend intelligence where the stakes are, and
nowhere else.** It is why the number is half a US cent, and why the safety
claims survive contact with a customer trying to break them.

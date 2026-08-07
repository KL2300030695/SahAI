# SahAI — Interface Design Proposal

> Status: **awaiting review.** No code written yet.

---

## 0. What's wrong with the current UI

I built it, so let me be direct about it.

- **Three equal columns give unequal things equal weight.** Transcript, assist,
  and cost ledger each take a third. The cost ledger is a *judge* concern; the
  agent will never look at it mid-call. The suggestion — the only thing they
  actually need — is one panel among nine.
- **It is the banned tell.** Near-black slate, single neon-ish accent, uppercase
  11px panel titles, rounded cards in a grid. That is the default AI dashboard.
- **Nothing encodes what the product is.** Swap the labels and it could be a
  logistics tracker.
- **The two moments that carry the product's whole safety story** — consent, and
  the approval gate — are a modal and a button.

---

## 1. The insight the design should come from

The agent is **talking**. Their attention is on a person, not a screen. They
glance for two seconds and look away.

And what they need in those two seconds is **a sentence to say out loud.**

That reframes everything. This is not a dashboard with a suggestion panel. It is
a **teleprompter with evidence attached**. The suggestion is the interface;
everything else exists to answer "can I trust this line?"

### The thesis

> **Two human moments bracket the call — the words you say, and the decision you
> sign. Everything else is evidence.**

Consent opens the call (a line the human speaks). Approval closes it (a decision
the human signs). Those two get the most typographic weight in the product,
because they are the two points where a human is actually in the loop. That is
the guardrail story told through layout rather than through a badge.

---

## 2. Signature element — **The Say Line**

One element, sized and styled unlike anything else in the product: a full-width
band holding exactly one sentence — what to say next — set in serif at speaking
size.

Its signature behaviour is **inline provenance**. Every figure in the sentence
that came from the knowledge base is marked *within the sentence itself*, not in
a citations panel underneath:

```
"There's no processing fee — you repay exactly ₹12,000 in three
                                               ┈┈┈┈┈┈┈
 instalments of ₹4,000, and the only charges are ₹250 if one is missed."
                 ┈┈┈┈┈┈                          ┈┈┈┈
```

Marked = traced to a cited chunk. Hover or focus reveals the document and
version. This is the product's core promise made visible at the exact moment it
matters — and it teaches the agent to trust marked figures and question unmarked
ones, which is precisely the habit the grounding guardrail enforces in code.

### Its four states

| State | Treatment | When |
|---|---|---|
| **Ready** | Teal figure-marks, calm | Grounded, safe to say |
| **Your call** | Amber left edge, "you confirm this before saying it" | Credit terms — human confirmation forced |
| **Held** | Line replaced by plain-language reason, not an error | Guardrail blocked it. Says *why*, in the interface's voice |
| **Listening** | Low-contrast placeholder, breathing | Between turns — deliberately not a spinner |

The **Held** state matters. The brief asks to surface uncertainty plainly. When
the system blocks a suggestion, the agent sees *"I couldn't source that fee — ask
the customer to hold while you check"*, not a red toast.

---

## 3. Colour

Five semantic colours, two neutrals. Each earns its place by *meaning something*
in this product, not by being an accent.

| Token | Hex | Meaning |
|---|---|---|
| `ink` | `#161B22` | Text. Near-black, slightly cool — reads cleanly at a glance. |
| `graphite` | `#5B6570` | Labels, secondary text, the "system talking" voice. |
| `paper` | `#F5F6F7` | App ground. Neutral, not cream. |
| `surface` | `#FFFFFF` | Raised areas — the Say Line, the approval band. |
| `verified` | `#0F7B6C` | Deep teal. "This is traced to a source." Used *only* for grounded figures and passed code checks. |
| `yourcall` | `#B45309` | Burnt amber. "A human decides this." Used *only* on human-confirmation and the approval gate. |
| `halt` | `#B42318` | Serious red. Blocked, injection, opt-out. Never used decoratively. |

**Live state is not a colour.** Broadcast convention says on-air is red, but red
is already "stop" here. So live is expressed as a slowly breathing graphite dot —
motion, not hue. That keeps the palette at five and avoids the collision.

### Why light, not dark

Dark UI with a neon accent is the specific tell the brief bans, and it is also
wrong for the room: call floors are brightly lit, and the two most-read surfaces
here are long-form text (transcript and Say Line), which read faster as dark-on-
light. Light also reads as financial-institution trust rather than developer
tool.

**Counter-argument, flagged honestly:** the demo runs on a projector, possibly in
a dim room, where dark UI often looks better. I think a high-contrast light
design projects fine — but this is a real trade-off and it is your call. See the
question at the end.

---

## 4. Type

The pairing carries meaning rather than being decoration.

| Role | Face | Why |
|---|---|---|
| **Speech** — Say Line, consent script | **Source Serif 4** | A serif marks *human language* against the system's sans. The one thing on screen a person will say aloud looks different from everything the machine reports. |
| **Interface** — labels, transcript, controls | **IBM Plex Sans** | Designed for technical interfaces, humane rather than corporate, and not Inter/Roboto. |
| **Data** — chunk ids, costs, tokens, versions | **IBM Plex Mono** | Same family, so the system's voice is one voice. |

The serif/sans split *is* the product's core distinction rendered
typographically: **what a human says** versus **what the machine knows**.

### Scale

| Use | Size / leading |
|---|---|
| Say Line | 23px / 1.45 serif — readable at arm's length in a glance |
| Consent script | 21px / 1.5 serif |
| Transcript | 15px / 1.55 sans |
| Body, controls | 13px sans |
| Labels | 11px sans, +0.06em tracking, graphite |
| Data | 12px mono |

Loaded from Google Fonts with `display=swap` and a full system fallback stack,
so a dead conference wifi degrades to system fonts rather than to nothing.

---

## 5. Layout

Horizontal bands, not columns — because the hierarchy is genuinely ordered, and
columns imply equality.

```
┌───────────────────────────────────────────────────────────────────────┐
│ ◉ live   Arun Menon · Bengaluru · KYC not started        02:14        │  on-air
├───────────────────────────────────────────────────────────────────────┤
│                                                                       │
│  SAY NEXT                                          ⌾ you confirm this │
│                                                                       │
│  "There's no processing fee — you repay exactly ₹12,000 in three      │  ← THE
│   instalments of ₹4,000. The only charges are ₹250 if one is missed."│    SAY
│                        ┈┈┈┈┈┈                     ┈┈┈┈                │    LINE
│                                                                       │
│  Answers the fee objection with the current schedule                  │
├─────────────────────────────────────┬─────────────────────────────────┤
│  CONVERSATION                       │  WHAT THIS USED                 │
│                                     │                                 │
│  customer  ▸ my friend said there's │  reading  objection_cost        │
│              a 199 processing fee   │  tone     sceptical             │
│                                     │                                 │
│  you       ▸ let me check that      │  source   Pricing & Fees v3.1   │
│                                     │           "Processing fee ₹0…"  │
│                                     │  ⚠ dropped 1 expired 2024 doc   │
├─────────────────────────────────────┴─────────────────────────────────┤
│  ✓ 8 checks · 6 enforced in code      $0.0032 · 57× cheaper      ⌄    │  evidence
└───────────────────────────────────────────────────────────────────────┘
```

- **On-air strip** — who you're on with and how long. Thin, always there.
- **Say Line** — the only large type on screen.
- **Conversation** — newest at the bottom, customer turns weighted heavier than
  the agent's own.
- **What this used** — the trust column. Intent, tone, and the source snippet
  *with its document name and version visible*, so it can't read as invented.
- **Evidence strip** — guardrails and cost, collapsed by default, expandable.
  Present for the judges, out of the agent's way. During the demo you expand it
  and it becomes the whole story.

**Critique against "is this any fintech dashboard?"** — No sidebar, no KPI cards,
no chart, no equal-column grid. The dominant element is a sentence. That shape
only makes sense for this product.

---

## 6. The two weighted moments

Both get the serif and the full width. This is the human-in-the-loop guardrail
expressed as layout.

**Consent** — a full-viewport moment, not a modal over a dimmed dashboard.
Nothing else on screen. The script is set at speaking size because the agent is
about to read it aloud. Below it, in graphite: *"The co-pilot will not process a
single turn until this is on record — it raises, it doesn't warn."* One primary
action, and a visible decline path that explains what happens instead.

**Approval** — the mirror image, closing the call. A full-width band, same
weight. Renders the CRM change as an actual `before → after` diff rather than a
JSON blob, with the amber `yourcall` edge. The agent's name is required, and the
UI states plainly: *nothing here has been written; you are the only thing that
can write it.* It should feel like signing, not like dismissing a dialog.

---

## 7. Idle / between calls

Orientation, not a blank page. Three things: which line you're on and that it's
listening; a one-line statement of what the co-pilot will do when a call starts;
and the consent script previewed in full, so the opening words are already in
front of the agent before the phone rings.

---

## 8. Copy

Agent's point of view, named by what they do with it.

| Now | Becomes |
|---|---|
| Suggested next action | **Say next** |
| Detected intent | **Reading** |
| Knowledge base | **Source** |
| Self-check | **Checks** |
| Requires human confirmation | **You confirm this before saying it** |
| Suggestion withheld | **Held — I couldn't source that. Ask them to hold while you check.** |
| Retrieval · $0 | *(moves to the evidence strip; the agent doesn't care)* |

Low confidence and weak retrieval are stated plainly in the same voice, never
hidden: *"Weak match — I'm not confident this answers what they asked."*

---

## 9. Motion

Restrained, and only where it carries information.

| Where | What | Why |
|---|---|---|
| New transcript line | 120ms fade + 4px rise | Shows arrival without pulling the eye off the Say Line |
| Say Line changes | 180ms cross-fade, no slide | A slide implies a queue; there is only ever one current line |
| Live dot | 2s breathe | The only looping animation in the product |
| Thinking | Say Line dims to 60% | Not a spinner — the agent should keep reading the old line until a new one is ready |

All of it behind `prefers-reduced-motion: reduce`, which drops every transition
to 0ms and stops the breathe.

---

## 10. Responsive

- **≥1200px** — as drawn.
- **900–1200px** — evidence columns stack under the conversation; Say Line
  keeps full width and full size.
- **<900px (tablet, between calls)** — single column. Say Line first, then
  conversation, then sources. The evidence strip becomes a sheet.

The Say Line never shrinks below 19px. If something has to give, it's the
transcript.

---

## 11. Trade-offs I'd be making

Flagged rather than hidden.

1. **Light theme is a genuine bet.** Better for a lit call floor and for reading;
   possibly worse on a dim-room projector. Your call — see below.
2. **Web fonts add a network dependency.** Mitigated with `display=swap` and a
   system fallback, but on dead wifi it degrades to system faces and loses some
   of the serif/sans distinction.
3. **Your Tailwind config currently sets fonts.** The brief says core utilities
   only, so families move into `index.css` and the config goes back to stock.
4. **Inline figure-marking needs the backend to return figure→chunk spans**, not
   just a list of chunk ids. Today the grounding check computes that internally
   and throws it away. Small change to `check_grounding` to return the mapping —
   worth doing, since it turns a passing check into a visible feature.
5. **This is a restyle of every screen**, not a re-skin. Roughly: tokens and
   shell, Say Line, live view, consent, approval, idle, responsive pass. I'd
   build in that order so the core demo moment is right first even if the tail
   gets cut.

---

## 12. Build order

1. Tokens + shell (fonts, colour, the band layout)
2. **Say Line** with inline provenance — the demo moment
3. Live call view around it
4. Consent moment
5. Approval band
6. Idle state
7. Evidence strip + responsive + reduced-motion pass

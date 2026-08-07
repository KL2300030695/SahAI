---
doc_id: call-consent-script
title: Mandatory Call Opening and Consent Script
version: v2.0
effective_from: 2026-04-01
category: compliance
---

# Mandatory Call Opening and Consent Script

## The opening (must be read before anything else)

> "Hi, this is {agent_name} calling from PayFlex. Before we start — this call may
>  be recorded and I'm using an AI assistant to help me pull up accurate
>  information while we talk. Is that alright with you?"

Nothing else may be discussed until the customer responds.

## Handling the response

**Consent given** — proceed. Log `consent_ack = true` with the timestamp.

**Consent refused** — offer the choice, do not argue:

> "That's completely fine. I can carry on without the recording and the
>  assistant, or call you back on a non-recorded line — whichever you prefer."

Log `consent_ack = false`. Recording and AI assistance must be disabled for the
remainder of the call.

**Customer asks what the AI does:**

> "It listens along and pulls up the exact product terms so I quote you the right
>  numbers rather than going from memory. It doesn't make any decision about your
>  application — that's the system's job after your KYC, and anything about your
>  terms comes from me, not it."

That answer is accurate and it is also the honest description of this system's
design. Do not embellish it.

## System enforcement

The SahAI orchestrator will not open a call session until `consent_ack` is
recorded. This is a code-level gate — the `consent_recorded` guardrail check —
not a reminder an agent can skip under time pressure.

## Closing

> "Thanks for your time. You'll get a summary on SMS shortly. If you'd rather we
>  didn't call again, just tell me and I'll mark it now."

Honour an opt-out immediately and log it.

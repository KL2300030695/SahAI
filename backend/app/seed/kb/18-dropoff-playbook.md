---
doc_id: dropoff-playbook
title: Drop-off Recovery Playbook
version: v2.5
effective_from: 2026-06-01
category: playbook
---

# Drop-off Recovery Playbook

## Signals that a live call is heading for drop-off

Treat any of these as elevated drop-off risk and adapt immediately:

- "Let me think about it" / "send me the details"
- Repeated questions about the same fee after it has been answered
- Going quiet after the Aadhaar or mandate step is mentioned
- "I'll do it later" when the app is already open
- Asking whether they can do it without the auto-debit mandate

## In-call response

Do not add pressure. Reduce the next step instead:

> "No problem at all. The one thing worth doing while we're on the line is the
>  first step — just the mobile OTP, takes about thirty seconds. Then it's saved
>  for a week and you can finish whenever suits you."

Getting step 1 completed on-call roughly doubles the odds of eventual completion
versus ending with nothing started.

## Post-call follow-up — timing

| Where they stopped | Send | When |
|---|---|---|
| Before starting KYC | SMS with resume link | +2 hours |
| Stopped at Aadhaar (step 3) | SMS addressing the second-OTP step directly | +4 hours |
| Stopped at mandate (step 4) | SMS explaining mandate cancellability | +4 hours |
| Completed KYC, no purchase | SMS with available limit reminder | +24 hours |
| Explicitly not interested | **Nothing** | — |

Progress is saved for 7 days. Follow-up after day 7 must acknowledge a restart.

## Follow-up content rules

- Address **the specific reason they stopped**. A generic "complete your
  application" message converts at a fraction of a targeted one.
- Include the resume link. Never include the customer's limit, PAN, or any part
  of their Aadhaar in an SMS.
- One follow-up per drop-off event. A second is permitted only if the customer
  replies.
- Never follow up with a customer who said they are not interested. This is a
  compliance matter under TRAI, not a matter of taste.

## Recording the reason

Log the *actual* stated reason, not the disposition. "Worried about credit score
impact", "wanted spouse's agreement", "merchant not covered" are reasons.
"Not interested" is not a reason — it is an outcome.

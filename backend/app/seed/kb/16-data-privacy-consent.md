---
doc_id: data-privacy-consent
title: Data Privacy and Consent Obligations
version: v2.0
effective_from: 2026-04-01
category: compliance
---

# Data Privacy and Consent Obligations

## Governing framework

Personal and financial data is handled under the **Digital Personal Data
Protection Act, 2023 (DPDP)**, alongside RBI outsourcing and customer-protection
guidance and applicable TRAI rules on commercial communication.

## Call recording and AI assistance

Every outbound and inbound sales call must open with a disclosure that the call
may be **recorded and AI-assisted**. This is mandatory, not best practice. The
exact wording is in `call-consent-script`.

If the customer declines recording, the agent must either continue with
recording and AI assistance disabled, or offer a callback. The customer's
decision is logged against the call record.

## Data minimisation on calls

Agents and any assisting system must not capture or store:

- Full Aadhaar number
- OTPs of any kind
- Card number, CVV, or PIN
- Net-banking credentials

Where such a value appears in a transcript, it must be **masked at rest and in
transit**, including in any AI assistance surface, log, or CRM note.

## Purpose limitation

Call transcripts and derived summaries may be used for servicing the customer,
quality assurance, and regulatory compliance. Any secondary use — including
model training — requires separate, specific consent.

## Customer rights

Customers may request access to, correction of, or erasure of their personal
data, and may withdraw consent. Route all such requests to the **Data Protection
Officer**. Sales agents must not action or refuse them directly.

## Retention

Call recordings and transcripts are retained for **24 months**, then deleted.
Derived CRM records follow the standard customer-record retention schedule.

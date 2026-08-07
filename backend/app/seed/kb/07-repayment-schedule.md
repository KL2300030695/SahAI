---
doc_id: repayment-schedule
title: Repayment Schedule and Auto-Debit
version: v3.0
effective_from: 2026-04-01
category: repayment
---

# Repayment Schedule and Auto-Debit

## The schedule

Three equal instalments, each exactly one third of cart value:

- **Instalment 1** — debited at checkout
- **Instalment 2** — debited on the same calendar date, +1 month
- **Instalment 3** — debited on the same calendar date, +2 months

If the checkout date does not exist in a later month (e.g. the 31st), the debit
falls on the **last day of that month**.

## Auto-debit mechanics

- Debits run at approximately **10:00 AM IST** on the due date.
- The customer receives an SMS and a push notification **3 days before** and
  again on the morning of each debit.
- On failure, PayFlex retries **once after 48 hours**. A bounce fee of ₹150
  applies per failed attempt, to a maximum of 2 attempts per cycle.

## Changing the debit date

The due date can be shifted **once per plan**, by up to **7 days**, from the app.
It must be requested at least 24 hours before the scheduled debit.

## Paying early

Any instalment can be paid early from the app at **no charge**. Paying the full
outstanding closes the plan immediately and frees the limit — see
`foreclosure-prepayment`.

## What the customer sees

The app shows the full schedule with exact dates and amounts before the customer
confirms the purchase. Nothing about the schedule is discovered later.

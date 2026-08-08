"""
Seed the mock CRM with the four customers who appear in the seed transcripts.

Run:  python -m app.seed.seed_db
"""

from __future__ import annotations

import json
from pathlib import Path

from app.crm.db import init_db, session_scope
from app.crm.models import Customer, Interaction

TRANSCRIPT_DIR = Path(__file__).resolve().parent / "transcripts"

# Addresses use the RFC 2606 reserved .invalid TLD on purpose: it can never
# resolve, so a misconfigured demo cannot deliver to a real person even if
# BREVO_REDIRECT_TO is forgotten. Set a real address here only for a customer
# you actually intend to email.
CUSTOMERS = [
    dict(
        customer_id="CUST-1042",
        name="Arun Menon",
        email="arun.menon@example.invalid",
        phone_masked="+91 98450 XXXXX",
        city="Bengaluru",
        kyc_status="not_started",
        kyc_last_step=0,
        credit_limit_inr=None,
        last_disposition="browsed_checkout",
        do_not_call=False,
        notes=[
            "Abandoned Pay-in-3 at checkout on a laptop purchase (cart ₹54,990).",
            "Opened the fee FAQ twice before leaving the page.",
        ],
    ),
    dict(
        customer_id="CUST-2318",
        name="Sneha Kulkarni",
        email="sneha.kulkarni@example.invalid",
        phone_masked="+91 99870 XXXXX",
        city="Pune",
        kyc_status="in_progress",
        kyc_last_step=3,  # stopped at Aadhaar e-KYC
        credit_limit_inr=None,
        last_disposition="kyc_abandoned",
        do_not_call=False,
        notes=[
            "Started KYC, dropped at step 3 (Aadhaar e-KYC).",
            "Previously asked support whether Aadhaar upload was mandatory.",
        ],
    ),
    dict(
        customer_id="CUST-3771",
        name="Deepak Iyer",
        email="deepak.iyer@example.invalid",
        phone_masked="+91 90080 XXXXX",
        city="Chennai",
        kyc_status="not_started",
        kyc_last_step=0,
        credit_limit_inr=None,
        last_disposition="enquiry",
        do_not_call=False,
        notes=[
            "Enquired about splitting a large-appliance purchase.",
            "Mentioned an upcoming home loan application — credit-score sensitive.",
        ],
    ),
    dict(
        customer_id="CUST-4506",
        name="Farhan Qureshi",
        email="farhan.qureshi@example.invalid",
        phone_masked="+91 97400 XXXXX",
        city="Hyderabad",
        kyc_status="not_started",
        kyc_last_step=0,
        credit_limit_inr=None,
        last_disposition="browsed_checkout",
        do_not_call=False,  # set to True by the call-004 approval flow
        notes=["Clicked the Pay-in-3 banner once; no application started."],
    ),
]


def seed() -> None:
    init_db()
    with session_scope() as s:
        for row in CUSTOMERS:
            notes = row.pop("notes")
            existing = s.get(Customer, row["customer_id"])
            if existing:
                for k, v in row.items():
                    setattr(existing, k, v)
                customer = existing
            else:
                customer = Customer(**row)
                s.add(customer)
                s.flush()
            if not customer.interactions:
                for n in notes:
                    s.add(Interaction(customer_id=customer.customer_id, note=n))
        print(f"seeded {len(CUSTOMERS)} customers")

    found = sorted(p.name for p in TRANSCRIPT_DIR.glob("*.json"))
    print(f"transcripts available ({len(found)}):")
    for name in found:
        data = json.loads((TRANSCRIPT_DIR / name).read_text(encoding="utf-8"))
        print(f"  {data['call_id']}  {data['outcome']:<15} {len(data['turns'])} turns")


if __name__ == "__main__":
    seed()

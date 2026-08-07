"""
Replay a seed transcript through the full pipeline and print the ledger.

    python run_call.py                      # call-001
    python run_call.py call-002             # by id
    python run_call.py call-004 --quiet     # ledger only

This is the fastest way to see the whole system work, and it is what produces
the cost-per-call figure quoted in the pitch — the number is measured from the
`usage` fields of real API responses, not estimated.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.config import get_settings
from app.crm.db import get_crm_snapshot, init_db, session_scope
from app.orchestrator import ConsentNotGiven, get_orchestrator
from app.schemas import Intent, Speaker, TranscriptTurn
from app.telemetry.cost import CostMeter, render_ledger_table

TRANSCRIPTS = Path(__file__).resolve().parent / "app" / "seed" / "transcripts"

DIM = "\033[2m"
BOLD = "\033[1m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RESET = "\033[0m"


def load(call_id: str) -> dict:
    for p in sorted(TRANSCRIPTS.glob("*.json")):
        data = json.loads(p.read_text(encoding="utf-8"))
        if data["call_id"] == call_id:
            return data
    available = [
        json.loads(p.read_text(encoding="utf-8"))["call_id"]
        for p in sorted(TRANSCRIPTS.glob("*.json"))
    ]
    raise SystemExit(f"unknown call id {call_id!r}. available: {', '.join(available)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("call_id", nargs="?", default="call-001")
    ap.add_argument("--quiet", action="store_true", help="ledger only")
    args = ap.parse_args()

    settings = get_settings()
    data = load(args.call_id)
    init_db()

    turns = [
        TranscriptTurn(
            index=t["index"],
            speaker=Speaker(t["speaker"]),
            text=t["text"],
            ts=float(t["ts"]),
        )
        for t in data["turns"]
    ]

    orch = get_orchestrator()
    meter = CostMeter(data["call_id"])

    mode = "MOCK" if settings.mock_mode else "LIVE (Groq)"
    print(f"\n{BOLD}SahAI — replaying {data['call_id']} [{mode}]{RESET}")
    print(f"{DIM}{data['scenario']}{RESET}\n")

    with session_scope() as s:
        crm = get_crm_snapshot(s, data["customer_id"])

    history: list[TranscriptTurn] = []
    intents_seen: list[Intent] = []
    max_risk = 0.0

    for turn in turns:
        try:
            assist = orch.handle_turn(
                call_id=data["call_id"],
                turn=turn,
                history=history,
                meter=meter,
                consent_ack=bool(data.get("consent_ack")),
                crm=crm,
            )
        except ConsentNotGiven as e:
            print(f"{RED}BLOCKED: {e}{RESET}")
            return 1

        history.append(turn)
        if turn.speaker != Speaker.CUSTOMER:
            continue
        if assist.intent:
            intents_seen.append(assist.intent.intent)
            max_risk = max(max_risk, assist.intent.dropoff_risk)

        if args.quiet:
            continue

        print(f"{BOLD}[{turn.index}] CUSTOMER:{RESET} {assist.turn.text}")

        if assist.blocked and not assist.nba:
            reason = assist.guardrail.blocked_reason if assist.guardrail else "blocked"
            print(f"  {RED}⛔ PIPELINE HALTED{RESET} {reason}")
            print(f"  {DIM}tiers: {' → '.join(assist.tier_path)}{RESET}\n")
            continue

        if assist.intent:
            i = assist.intent
            print(
                f"  {CYAN}intent{RESET}   {i.intent.value} "
                f"{DIM}(conf {i.confidence:.2f}, dropoff {i.dropoff_risk:.2f}){RESET}"
            )
        if assist.retrieval:
            ids = [c.chunk_id for c in assist.retrieval.citations]
            print(f"  {CYAN}kb{RESET}       {', '.join(ids) or '(none)'}")
            if assist.retrieval.dropped_stale:
                print(
                    f"  {YELLOW}stale{RESET}    dropped "
                    f"{', '.join(assist.retrieval.dropped_stale)}"
                )
        if assist.nba:
            flag = (
                f" {YELLOW}[needs human confirmation]{RESET}"
                if assist.nba.requires_human_confirmation
                else ""
            )
            print(f"  {GREEN}suggest{RESET}  {assist.nba.say}{flag}")
            print(f"  {DIM}why      {assist.nba.why}{RESET}")
        if assist.guardrail:
            for c in assist.guardrail.checks:
                mark = f"{GREEN}✓{RESET}" if c.passed else f"{RED}✗{RESET}"
                print(f"    {mark} {c.name.value:<28} {DIM}[{c.enforced_by}] {c.detail[:88]}{RESET}")
            if not assist.guardrail.passed:
                print(f"  {RED}⛔ SUGGESTION BLOCKED{RESET} {assist.guardrail.blocked_reason}")
        print(f"  {DIM}tiers: {' → '.join(assist.tier_path)}  "
              f"latency {assist.latency_ms:.0f}ms{RESET}\n")

    # ---- post-call ----
    with session_scope() as s:
        from app.crm.db import is_do_not_call

        dnc = is_do_not_call(s, data["customer_id"])

    print(f"{BOLD}── post-call ──{RESET}")
    result = orch.finalise_call(
        call_id=data["call_id"],
        transcript=turns,
        intents_seen=intents_seen,
        max_dropoff_risk=max_risk,
        meter=meter,
        consent_ack=bool(data.get("consent_ack")),
        crm=crm,
        do_not_call=dnc,
    )
    c = result.crm
    print(f"  {CYAN}disposition{RESET}  {c.disposition.value}")
    print(f"  {CYAN}summary{RESET}      {c.summary}")
    if c.dropoff_reason:
        print(f"  {CYAN}reason{RESET}       {c.dropoff_reason}")
    print(f"  {CYAN}crm_patch{RESET}    {c.crm_patch}")
    if c.followup_draft:
        print(f"  {CYAN}follow-up{RESET}    [{c.followup_draft.channel}] {c.followup_draft.body}")
    elif c.disposition.value == "not_interested" or dnc:
        print(f"  {CYAN}follow-up{RESET}    {YELLOW}SUPPRESSED BY COMPLIANCE{RESET} "
              f"{DIM}(customer opted out / do-not-call flag){RESET}")
    else:
        print(f"  {CYAN}follow-up{RESET}    {DIM}none needed ({c.disposition.value}){RESET}")
    print(f"  {CYAN}send_status{RESET}  {c.send_status.value} "
          f"{DIM}← requires a named human approver to advance{RESET}")

    for chk in result.guardrail.checks:
        mark = f"{GREEN}✓{RESET}" if chk.passed else f"{RED}✗{RESET}"
        print(
            f"    {mark} {chk.name.value:<28} "
            f"{DIM}[{chk.enforced_by}] {chk.detail[:96]}{RESET}"
        )
    if not result.guardrail.passed:
        print(f"  {RED}⛔ POST-CALL ARTEFACT BLOCKED{RESET} {result.guardrail.blocked_reason}")

    print(render_ledger_table(result.ledger))
    print(f"  {BOLD}{meter.summary_line()}{RESET}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
Drive one call through every stage and print the trace.

Run:  python -m scripts.run_full_pipeline [call_id]

This is the end-to-end demonstration: consent gate, then per-turn injection
screen -> intent -> retrieval -> next-best-action -> self-check, then post-call
summarisation, CRM patch proposal, follow-up draft and the post-call guardrail,
and finally the approval gate. Nothing is simulated -- it uses the real HTTP
surface, so what it prints is what the dashboard saw.

It stops at the approval gate on purpose. That gate is the automation boundary,
and a script that clicked through it would be misrepresenting the system.
"""

from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("PLAYBACK_INTERVAL_SECONDS", "0.05")

from fastapi.testclient import TestClient  # noqa: E402

from app.crm.db import session_scope  # noqa: E402
from app.export import (  # noqa: E402
    CALL_COLUMNS,
    TRACE_COLUMNS,
    call_rows,
    to_csv,
    trace_rows,
)
from app.main import app  # noqa: E402

CALL_ID = sys.argv[1] if len(sys.argv) > 1 else "call-001"

#: With AUTH_ENABLED=1 the write endpoints require a credential. Taken from the
#: environment rather than hard-coded so this script never carries a key, and
#: falls back to the first configured one so a demo run needs no extra setup.
def _key() -> dict:
    from app.config import get_settings

    explicit = os.environ.get("SAHAI_API_KEY")
    if explicit:
        return {"X-API-Key": explicit}
    first = (get_settings().api_keys or "").split(",")[0].split(":")[0].strip()
    return {"X-API-Key": first} if first else {}
OUT = "exports"


def main() -> None:
    client = TestClient(app, headers=_key())

    print(f"=== {CALL_ID} ===\n")
    r = client.post(
        f"/api/calls/{CALL_ID}/consent",
        json={"consent_ack": True, "agent_name": "Priya"},
    )
    r.raise_for_status()
    print(f"[gate 0] consent recorded — nothing runs before this\n")

    turns = assists = 0
    with client.websocket_connect(f"/ws/call/{CALL_ID}") as ws:
        while True:
            try:
                m = ws.receive_json()
            except Exception:
                break
            t = m.get("type")
            if t == "turn":
                turns += 1
                who = m["turn"]["speaker"]
                print(f"  {who:>8}: {m['turn']['text'][:88]}")
            elif t == "assist":
                a = m["assist"]
                if a.get("nba"):
                    assists += 1
                    tiers = " -> ".join(a.get("tier_path", []))
                    print(f"           [{tiers}]")
                    print(f"           say: {a['nba']['say'][:88]}")
                    if a.get("blocked"):
                        print(f"           BLOCKED: {a['guardrail']['blocked_reason']}")
                    print()
            elif t == "call_ended":
                break

    print(f"\n[live] {turns} turns, {assists} suggestions\n")

    r = client.post(f"/api/calls/{CALL_ID}/finalise")
    r.raise_for_status()
    post = r.json()
    crm = post["crm"]
    print("[post-call]")
    print(f"  disposition   {crm['disposition']}")
    print(f"  drop_off      {bool(crm.get('dropoff_reason'))}  {crm.get('dropoff_reason') or ''}")
    print(f"  crm_patch     {crm['crm_patch']}")
    print(f"  followup      {(crm.get('followup_draft') or {}).get('channel', 'none')}")
    print(f"  send_status   {crm['send_status']}")
    print(f"  guardrail     passed={post['guardrail']['passed']}")
    for c in post["guardrail"]["checks"]:
        mark = "ok  " if c["passed"] else "FAIL"
        print(f"     {mark} {c['name']:<28} [{c['enforced_by']}]")

    print("\n[automation boundary]")
    print("  written automatically : call log, summary, disposition, proposed patch")
    print("  requires a human      : customer-record mutation, sending the message")
    print(f"  current state         : {crm['send_status']}")

    # ---- the trace ----
    with session_scope() as s:
        trace = list(trace_rows(s, CALL_ID))
        calls_csv = to_csv(CALL_COLUMNS, call_rows(s, CALL_ID))
        trace_csv = to_csv(TRACE_COLUMNS, trace)

    print(f"\n[trace] {len(trace)} stages\n")
    print(f"  {'stage':<22}{'tier':<10}{'tok':>7}{'usd':>12}  detail")
    print("  " + "-" * 108)
    total = 0.0
    for row in trace:
        total += float(row["usd"])
        tok = int(row["prompt_tokens"]) + int(row["completion_tokens"])
        print(
            f"  {row['stage']:<22}{row['tier']:<10}{tok:>7}{float(row['usd']):>12.6f}"
            f"  {str(row['detail'])[:70]}"
        )
    print("  " + "-" * 108)
    print(f"  {'TOTAL':<22}{'':<10}{'':>7}{total:>12.6f}")

    frontier = post.get("frontier_usd") or 0.0
    if frontier and total > 0:
        print(f"\n  frontier baseline {frontier:.6f}  =>  {frontier / total:.1f}x cheaper")
    elif total == 0:
        # Mock mode bills nothing, so there is no ratio to report. Saying so is
        # better than printing a number that would be meaningless.
        print("\n  (mock mode — no tokens billed, so no cost ratio to report)")

    os.makedirs(OUT, exist_ok=True)
    for name, body in (("calls", calls_csv), ("trace", trace_csv)):
        path = os.path.join(OUT, f"sahai-{name}-{CALL_ID}.csv")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(body)
        print(f"\n  wrote {path}")


if __name__ == "__main__":
    main()

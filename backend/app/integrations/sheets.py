"""
Google Sheets export.

Writes the *same rows* the CSV export produces, to the same column order, into
two tabs of one spreadsheet:

    Calls   one row per call    (CALL_COLUMNS)
    Trace   one row per stage   (TRACE_COLUMNS)

Reusing `app.export`'s row builders is the whole design. A separate Sheets
serialiser would drift from the CSV within a week and the two would quietly
disagree about what a call cost -- and a judge comparing the file to the sheet
would find the discrepancy before we did.

Upsert, not append. A call is keyed by `call_id` in column A: replaying a
scenario overwrites its row rather than adding a second one, matching the
behaviour of the local ledger, which clears a scenario's rows when it restarts.
Without that, a demo rehearsed three times shows three contradictory rows for
the same call.

Like the Firestore mirror, this is best-effort and never fails a call.
"""

from __future__ import annotations

import threading
from typing import Any, Optional

from app.config import get_settings
from app.export import CALL_COLUMNS, TRACE_COLUMNS

CALLS_TAB = "Calls"
TRACE_TAB = "Trace"

_client: Any = None
_lock = threading.Lock()
_stats = {"rows": 0, "failures": 0, "last_error": ""}

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]


def enabled() -> bool:
    s = get_settings()
    return bool(s.sheets_enabled) and s.google_ready and bool(s.sheets_id)


def _get_client() -> Any:
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is not None:
            return _client
        import gspread
        from google.oauth2 import service_account

        s = get_settings()
        creds = service_account.Credentials.from_service_account_file(
            s.google_credentials_path, scopes=SCOPES
        )
        _client = gspread.authorize(creds)
        return _client


def _sheet():
    return _get_client().open_by_key(get_settings().sheets_id)


def _tab(book, title: str, columns: list[str]):
    """Fetch a tab, creating it with a header row if it isn't there yet."""
    try:
        ws = book.worksheet(title)
    except Exception:
        ws = book.add_worksheet(title=title, rows=1000, cols=max(len(columns), 26))
        ws.update([columns], "A1")
        ws.freeze(rows=1)
        return ws

    if not ws.row_values(1):
        ws.update([columns], "A1")
        ws.freeze(rows=1)
    return ws


def _as_row(columns: list[str], row: dict[str, Any]) -> list[str]:
    # Sheets has no notion of None; empty string is what a blank cell is.
    return ["" if row.get(c) is None else str(row.get(c, "")) for c in columns]


def status() -> dict[str, Any]:
    s = get_settings()
    return {
        "enabled": bool(s.sheets_enabled),
        "credentials_found": s.google_ready,
        "sheet_id_set": bool(s.sheets_id),
        "ready": enabled(),
        "url": (
            f"https://docs.google.com/spreadsheets/d/{s.sheets_id}"
            if s.sheets_id
            else None
        ),
        **_stats,
    }


def _record_failure(e: Exception) -> None:
    _stats["failures"] += 1
    _stats["last_error"] = f"{type(e).__name__}: {e}"[:300]


def push_call(
    call_row: dict[str, Any], stage_rows: list[dict[str, Any]]
) -> Optional[str]:
    """Upsert one call row and replace its trace rows. Returns the sheet URL."""
    if not enabled():
        return None
    try:
        book = _sheet()
        call_id = str(call_row.get("call_id") or "").strip()
        if not call_id:
            return None

        calls = _tab(book, CALLS_TAB, CALL_COLUMNS)
        ids = calls.col_values(1)  # includes the header
        payload = _as_row(CALL_COLUMNS, call_row)
        if call_id in ids[1:]:
            r = ids.index(call_id) + 1
            calls.update([payload], f"A{r}")
        else:
            calls.append_row(payload, value_input_option="RAW")
        _stats["rows"] += 1

        if stage_rows:
            trace = _tab(book, TRACE_TAB, TRACE_COLUMNS)
            # Drop this call's previous stages so a replay does not double up.
            existing = trace.col_values(2)  # call_id column
            stale = [i + 1 for i, v in enumerate(existing[1:], start=1) if v == call_id]
            for r in sorted(stale, reverse=True):
                trace.delete_rows(r)
            trace.append_rows(
                [_as_row(TRACE_COLUMNS, s) for s in stage_rows],
                value_input_option="RAW",
            )
            _stats["rows"] += len(stage_rows)

        return f"https://docs.google.com/spreadsheets/d/{get_settings().sheets_id}"
    except Exception as e:  # noqa: BLE001 - never fail a call over a spreadsheet
        _record_failure(e)
        return None


def reset_for_tests() -> None:
    global _client
    _client = None
    _stats.update({"rows": 0, "failures": 0, "last_error": ""})

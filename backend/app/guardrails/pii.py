"""
PII redaction.

Deterministic regex, applied to every transcript turn, WebSocket frame, log
line, and CRM write. Not a prompt instruction — a model asked nicely to redact
will comply most of the time, and "most of the time" is not a data-protection
posture under the DPDP Act.

Order matters: Aadhaar and card numbers are matched before the generic phone
pattern, since a 12-digit Aadhaar contains digit runs a loose phone regex would
otherwise claim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# --- patterns ---------------------------------------------------------------

# Aadhaar: 12 digits, commonly spoken/written in 4-4-4 groups.
AADHAAR = re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}\b")

# Card: 13-19 digits in groups.
CARD = re.compile(r"\b(?:\d{4}[\s-]?){3}\d{1,7}\b")

# PAN: 5 letters, 4 digits, 1 letter.
PAN = re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b", re.IGNORECASE)

# Indian mobile: optional +91/0 prefix, leading 6-9, 10 digits, loose spacing.
PHONE = re.compile(r"(?:(?:\+?91|0)[\s-]?)?\b[6-9]\d{4}[\s-]?\d{5}\b")

EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

# "OTP is 483920", "otp 4839"
OTP = re.compile(r"\b(?:otp|o\.t\.p\.?|one[\s-]?time[\s-]?password)\b[^\d]{0,12}(\d{4,8})", re.IGNORECASE)

CVV = re.compile(r"\b(?:cvv|cvc)\b[^\d]{0,8}(\d{3,4})", re.IGNORECASE)

# Numbers spoken as words are common on calls ("nine eight four five zero...").
_DIGIT_WORDS = r"(?:zero|one|two|three|four|five|six|seven|eight|nine|oh|double|triple)"
SPOKEN_DIGITS = re.compile(
    rf"\b(?:{_DIGIT_WORDS}[\s,-]+){{7,}}{_DIGIT_WORDS}\b", re.IGNORECASE
)

# Ordered longest-match-first. CARD must precede AADHAAR: a 16-digit card's
# first 12 digits match the Aadhaar pattern, so with Aadhaar first the card is
# only partially masked and the last four digits leak
# ("card [AADHAAR_REDACTED] 1111"). CARD requires 13+ digits so it never
# swallows a genuine 12-digit Aadhaar. Caught by a test, not by review.
_RULES: list[tuple[str, re.Pattern[str], str]] = [
    ("card", CARD, "[CARD_REDACTED]"),
    ("aadhaar", AADHAAR, "[AADHAAR_REDACTED]"),
    ("pan", PAN, "[PAN_REDACTED]"),
    ("otp", OTP, "[OTP_REDACTED]"),
    ("cvv", CVV, "[CVV_REDACTED]"),
    ("phone", PHONE, "[PHONE_REDACTED]"),
    ("email", EMAIL, "[EMAIL_REDACTED]"),
    ("spoken_digits", SPOKEN_DIGITS, "[DIGITS_REDACTED]"),
]


@dataclass
class RedactionResult:
    text: str
    found: list[str]

    @property
    def changed(self) -> bool:
        return bool(self.found)


def redact(text: str) -> RedactionResult:
    """Mask PII. Returns the safe text and the kinds of PII removed."""
    if not text:
        return RedactionResult(text="", found=[])

    out = text
    found: list[str] = []
    for name, pattern, placeholder in _RULES:
        if name in ("otp", "cvv"):
            # Replace only the captured digits so the surrounding sentence
            # ("the OTP is ...") stays readable to a reviewer.
            def _sub(m: re.Match[str], ph: str = placeholder) -> str:
                return m.group(0).replace(m.group(1), ph)

            new = pattern.sub(_sub, out)
        else:
            new = pattern.sub(placeholder, out)
        if new != out:
            found.append(name)
            out = new

    return RedactionResult(text=out, found=found)


def redact_text(text: str) -> str:
    return redact(text).text


def scan(text: str) -> list[str]:
    """Report PII kinds present without modifying the text."""
    return [name for name, pattern, _ in _RULES if pattern.search(text or "")]


def redact_obj(obj: object) -> object:
    """Recursively redact strings inside dicts/lists. Used on CRM patches and
    WebSocket payloads before they leave the process."""
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        return {k: redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    return obj

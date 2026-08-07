"""
Settings, the model-tier table, and the pricing table.

PRICING is the single place cost numbers are defined. The ledger multiplies real
`usage` from each API response by these rates, so the cost-per-call figure in the
pitch is measured rather than asserted. If you change a model in .env, the
reported cost changes with it.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.schemas import ModelTier

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    groq_api_key: str = ""

    model_tiny: str = "meta-llama/llama-prompt-guard-2-86m"
    model_cheap: str = "llama-3.1-8b-instant"
    model_standard: str = "openai/gpt-oss-20b"
    model_high: str = "openai/gpt-oss-120b"
    model_safety: str = "openai/gpt-oss-safeguard-20b"
    model_stt: str = "whisper-large-v3-turbo"

    sahai_mock: int = 0
    playback_interval_seconds: float = 3.5

    database_url: str = "sqlite:///./data/sahai.db"
    chroma_dir: str = "./app/rag/store"
    cors_origins: str = "http://localhost:5173"

    # --- Google (Firestore + Sheets) -------------------------------------
    # One service-account JSON serves both when they live in the same GCP or
    # Firebase project, which is the setup the README describes. Every field
    # here is optional: unset, the app runs exactly as it does today, writing
    # only to SQLite and CSV. Nothing about the demo depends on the network.
    google_credentials_path: str = ""
    firestore_enabled: int = 0
    firestore_project: str = ""
    #: Prefix so several people can share one Firestore project without
    #: overwriting each other's calls during a hackathon.
    firestore_prefix: str = "sahai"
    #: Only needed when Sheets lives in a different GCP project from
    #: Firestore. Blank falls back to GOOGLE_CREDENTIALS_PATH.
    sheets_credentials_path: str = ""
    sheets_enabled: int = 0
    #: The long id from the sheet URL: docs.google.com/spreadsheets/d/<THIS>/edit
    sheets_id: str = ""

    def _resolve_credentials(self, raw: str) -> Optional[Path]:
        """Absolute path to a service-account JSON, or None.

        Resolved against the repo root rather than the process CWD. uvicorn is
        started from `backend/`, so a natural-looking `./secrets/key.json` in a
        root-level .env otherwise resolves to `backend/secrets/key.json` and the
        integration silently reports "no credentials" while the file sits right
        there. The sqlite URL already needed the same treatment.
        """
        raw = (raw or "").strip()
        if not raw:
            return None
        p = Path(raw)
        if p.is_absolute():
            return p if p.exists() else None
        for base in (REPO_ROOT, BACKEND_DIR):
            candidate = (base / p).resolve()
            if candidate.exists():
                return candidate
        return None

    @property
    def google_credentials_file(self) -> Optional[Path]:
        """Credentials for Firestore, and the default for everything else."""
        return self._resolve_credentials(self.google_credentials_path)

    @property
    def sheets_credentials_file(self) -> Optional[Path]:
        """Credentials for Sheets, falling back to the shared one.

        Separate because the two services need not live in the same GCP project
        -- Firestore comes with a Firebase project, while a Sheets service
        account is often created standalone. Assuming one file for both meant
        enabling Sheets silently re-pointed Firestore at the wrong project.
        """
        return (
            self._resolve_credentials(self.sheets_credentials_path)
            or self.google_credentials_file
        )

    @property
    def google_ready(self) -> bool:
        return self.google_credentials_file is not None

    @property
    def sheets_ready(self) -> bool:
        return self.sheets_credentials_file is not None

    usd_to_inr: float = 83.0

    @property
    def mock_mode(self) -> bool:
        # Mock if explicitly asked, or if there is simply no key to call with.
        return bool(self.sahai_mock) or not self.groq_api_key

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def model_for(self, tier: ModelTier) -> str:
        return {
            ModelTier.TINY: self.model_tiny,
            ModelTier.CHEAP: self.model_cheap,
            ModelTier.STANDARD: self.model_standard,
            ModelTier.HIGH: self.model_high,
            ModelTier.SAFETY: self.model_safety,
            ModelTier.STT: self.model_stt,
        }.get(tier, "")


@lru_cache
def get_settings() -> Settings:
    return Settings()


# ---------------------------------------------------------------------------
# Pricing: USD per MILLION tokens, from Groq's published model pricing.
# ---------------------------------------------------------------------------

PRICING: dict[str, tuple[float, float]] = {
    # model id: (input $/Mtok, output $/Mtok)
    "meta-llama/llama-prompt-guard-2-86m": (0.035, 0.035),
    "meta-llama/llama-prompt-guard-2-22m": (0.030, 0.030),
    "llama-3.1-8b-instant": (0.05, 0.08),
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "openai/gpt-oss-20b": (0.075, 0.30),
    "openai/gpt-oss-120b": (0.15, 0.60),
    "openai/gpt-oss-safeguard-20b": (0.075, 0.30),
    "qwen/qwen3.6-27b": (0.60, 3.00),
}

# Whisper bills per hour of audio, not per token.
STT_USD_PER_HOUR: dict[str, float] = {
    "whisper-large-v3-turbo": 0.04,
    "whisper-large-v3": 0.111,
}

# Reference point for the pitch's cost comparison: what the same token volume
# would cost through a single frontier-model mega-prompt.
FRONTIER_BASELINE = {
    "label": "Claude Opus 5 (single mega-prompt, no RAG, no tiering)",
    "input_usd_per_mtok": 5.0,
    "output_usd_per_mtok": 25.0,
}


def price_tokens(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """USD cost of one call. Unknown models price at 0 rather than guessing."""
    rate_in, rate_out = PRICING.get(model, (0.0, 0.0))
    return (prompt_tokens * rate_in + completion_tokens * rate_out) / 1_000_000


def price_audio_seconds(model: str, seconds: float) -> float:
    return STT_USD_PER_HOUR.get(model, 0.0) * (seconds / 3600.0)


# ---------------------------------------------------------------------------
# Business goals. The self-check agent adjudicates every customer-facing and
# CRM-writing output against this text. Kept here, in one place, so the goals
# the system checks itself against are reviewable rather than buried in a prompt.
# ---------------------------------------------------------------------------

BUSINESS_GOALS = """\
1. Help the customer genuinely understand the pay-in-3 zero-cost EMI offer:
   what they pay, when, and what it costs them (nothing extra, if eligible).
2. Move qualified customers toward completing KYC and activating the offer.
3. Never overstate eligibility, credit limit, tenure, or fees. Accuracy beats
   conversion every single time.
4. Surface the human agent's judgement on anything sensitive -- final credit
   terms, eligibility decisions, disputes. The assistant suggests; the human
   decides and speaks.
5. Respect the customer's time and stated wishes. If they decline, capture the
   reason cleanly and stop selling.
6. Keep every claim traceable to the product knowledge base. No invented terms,
   no remembered terms, no rounded-off numbers.
"""

# Regex-able signals that an output is touching regulated credit territory and
# must therefore carry a human-confirmation flag. Used by guardrails/rules.py.
CREDIT_TERM_PATTERNS = [
    r"\binterest\s+rate\b",
    r"\bAPR\b",
    r"\bcredit\s+limit\b",
    r"\bsanction(ed)?\b",
    r"\bapprove[ds]?\b",
    r"\beligib(le|ility)\b",
    r"\btenure\b",
    r"\bEMI\s+of\b",
    r"\bprocessing\s+fee\b",
    r"\blate\s+fee\b",
    r"\bpenalt(y|ies)\b",
    r"\bforeclos(e|ure)\b",
    r"\bcredit\s+score\b",
    r"\bguarantee[ds]?\b",
]


def ensure_runtime_dirs() -> None:
    (BACKEND_DIR / "data").mkdir(parents=True, exist_ok=True)
    (BACKEND_DIR / "uploads").mkdir(parents=True, exist_ok=True)
    s = get_settings()
    Path(os.path.join(BACKEND_DIR, s.chroma_dir)).mkdir(parents=True, exist_ok=True)

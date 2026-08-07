"""
Groq client wrapper.

Everything the rest of the app knows about the LLM provider lives behind this
module, so swapping provider is a one-file change rather than a grep across the
agents. Each call returns real `usage` from the API response; the router turns
that into a priced ledger row, which is why the cost figure in the pitch is
measured rather than estimated.

Mock mode returns deterministic canned payloads without touching the network.
It exists so a live demo survives a rate limit or a dead conference wifi, and it
still exercises the real orchestrator and the real code-level guardrails.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from app.config import get_settings, price_tokens
from app.schemas import ModelTier

_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _supports_reasoning_effort(model: str) -> bool:
    """Only the gpt-oss family accepts `reasoning_effort`; llama models 400 on it."""
    return "gpt-oss" in model


# ---------------------------------------------------------------------------
# Speech-to-text priming
# ---------------------------------------------------------------------------

# Whisper defaults to US conventions and mangles Indian fintech speech in ways
# that matter downstream. Measured on the same clip of a customer saying
# "a one ninety nine processing fee":
#
#   no prompt / generic prompt  ->  "a $1.99 processing fee"
#   this prompt                 ->  "a 199 processing fee"
#
# That is not cosmetic. "$1.99" is the wrong currency AND the wrong magnitude,
# and the grounding guardrail matches figures against retrieved chunk text --
# so a mis-transcribed amount either fails to match anything or, worse, matches
# the wrong thing. Priming the decoder is the cheapest fix available: it costs
# nothing and happens before any reasoning model sees the turn.
STT_PROMPT = (
    "Indian fintech inside-sales call about PayFlex Pay-in-3, a zero-cost EMI "
    "product. All money amounts are in Indian rupees and must be written with "
    "the rupee symbol, never a dollar sign. Spoken numbers like 'one ninety "
    "nine' mean 199 and 'two fifty' means 250. Domain terms: Aadhaar, PAN, KYC, "
    "e-KYC, OTP, UPI AutoPay, e-NACH mandate, instalment, EMI, CIBIL, lakh."
)

# Belt and braces for the amounts the prompt does not catch. This product quotes
# no USD amount anywhere -- every figure in the knowledge base is in rupees -- so
# a dollar sign in a transcript is always a speech-recognition artefact, never a
# real quantity. Rewriting it keeps the grounding check comparing like with like.
_DOLLAR = re.compile(r"\$\s?(\d[\d,]*)(?:\.(\d{2}))?\b")

# Whisper also renders "one ninety nine" as "1-99" and "two fifty" as "2-50" --
# it hears the compound as two spoken groups and hyphenates them. Narrowly
# scoped to 1-2 digits followed by exactly 2, which covers the spoken hundreds
# forms this domain uses without touching a year range or a version number.
_SPLIT_NUMBER = re.compile(r"(?<![\w.-])(\d{1,2})-(\d{2})(?![\w.-])")


def normalise_currency(text: str) -> str:
    """Repair the two ways Whisper mangles spoken Indian rupee amounts.

    `$1.99` becomes `₹199`: Whisper writes the decimal form when it hears
    "one ninety nine" as a price, so the digits are right and only the placement
    is wrong. A genuine decimal (`$12.50` -> `₹12.50`) is preserved when the
    integer part has more than one digit, since that reads as a real amount
    rather than a misheard hundreds figure.

    `1-99` becomes `199`, likewise.

    Neither of these is a guarantee, and they are not meant to be. Speech
    recognition on numbers is probabilistic and the output varies between runs
    on identical audio. The actual safety property comes from the grounding
    guardrail downstream: a figure that survives mis-transcription cannot be
    quoted back to the customer, because it will not match any retrieved chunk.
    These normalisations reduce how often a good turn is wasted; grounding is
    what stops a bad one reaching a human.
    """
    def repl(m: re.Match[str]) -> str:
        whole, cents = m.group(1), m.group(2)
        if cents and len(whole.replace(",", "")) == 1:
            return f"₹{whole}{cents}"  # $1.99 -> ₹199
        return f"₹{whole}" + (f".{cents}" if cents else "")

    return _SPLIT_NUMBER.sub(r"\1\2", _DOLLAR.sub(repl, text))


# ---------------------------------------------------------------------------
# Hallucination filtering
# ---------------------------------------------------------------------------

# Whisper does not return "nothing" for silence -- it returns its training
# priors. Fed room tone, a breath, or line noise it emits confident filler,
# overwhelmingly the sign-offs that end the YouTube captions it was trained on.
#
# Observed in a real session: a transcript pane filled with "Thank you.",
# "I'm not sure." and "Spoken up." for turns where nobody had spoken.
#
# This is not cosmetic. Every accepted turn enters conversation history and is
# fed to the intent classifier on every subsequent turn, so a run of phantom
# filler drags classification toward `smalltalk` and buries the real question.
# A length check alone cannot catch it: "Thank you." is ten characters.
_HALLUCINATED_FILLER = {
    "thank you", "thank you very much", "thanks", "thanks for watching",
    "thank you for watching", "thanks for watching!", "please subscribe",
    "subscribe", "like and subscribe", "bye", "bye bye", "goodbye",
    "you", "yeah", "uh", "um", "hmm", "mm", "mhm", "okay", "ok", "so",
    "i'm not sure", "im not sure", "spoken up", "silence", "no",
    "music", "applause", "laughter", "beep", "blank audio", "inaudible",
    "transcription by castingwords", "the end", "end of transcript",
}

# Bracketed sound tags: [MUSIC], (upbeat music), ♪ ... ♪
_SOUND_TAG = re.compile(r"^\s*[\[\(♪].*[\]\)♪]\s*$")
_PUNCT_ONLY = re.compile(r"^[\W_]*$", re.UNICODE)


def is_probably_hallucination(text: str, audio_seconds: float = 0.0) -> bool:
    """True when a transcript is almost certainly Whisper filling in silence.

    Deliberately conservative about *what* it matches but not about *when*: it
    only rejects an utterance whose entire content is a known filler phrase, so
    a customer saying "thank you, that's much clearer" is untouched while a bare
    "Thank you." on a silent clip is dropped.

    Dropping a real one-word acknowledgement costs nothing -- it carries no
    intent and would route to `smalltalk` anyway. Accepting a phantom one
    corrupts every classification that follows.
    """
    stripped = (text or "").strip()
    if len(stripped) < 3:
        return True
    if _PUNCT_ONLY.match(stripped) or _SOUND_TAG.match(stripped):
        return True

    normalised = re.sub(r"[^\w\s']", "", stripped).strip().lower()
    normalised = re.sub(r"\s+", " ", normalised)
    if normalised in _HALLUCINATED_FILLER:
        return True

    # Whisper cannot produce a real sentence from a fraction of a second; a long
    # transcript off a very short clip is fabricated.
    if 0 < audio_seconds < 0.8 and len(stripped) > 12:
        return True

    return False


@dataclass
class LLMResponse:
    text: str
    model: str
    tier: ModelTier
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    usd: float = 0.0
    mocked: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    def json(self, default: Optional[dict] = None) -> dict[str, Any]:
        """Parse the response as JSON, tolerating prose or fences around it.

        A malformed payload returns `default` rather than raising: a single
        badly-formatted model response should degrade one panel of the dashboard,
        not tear down the call.
        """
        text = self.text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
        try:
            return json.loads(text)
        except Exception:
            pass
        m = _JSON_BLOCK.search(text)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                pass
        return default if default is not None else {}


class LLMClient:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._client = None
        if not self.settings.mock_mode:
            from groq import Groq

            self._client = Groq(api_key=self.settings.groq_api_key)

    @property
    def mock(self) -> bool:
        return self.settings.mock_mode

    # -- chat ------------------------------------------------------------

    def complete(
        self,
        tier: ModelTier,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        max_tokens: int = 900,
        temperature: float = 0.2,
        reasoning_effort: Optional[str] = "low",
        mock_payload: Optional[dict | str] = None,
    ) -> LLMResponse:
        """One chat completion.

        `reasoning_effort` is the third cost lever in this system, alongside
        model tiering and RAG. The gpt-oss models are reasoning models: their
        chain-of-thought is billed as completion tokens at the output rate, so
        on `gpt-oss-120b` ($0.60/Mtok out) the setting moves real money.
        Measured on an identical next-best-action prompt: effort=low produced
        150 completion tokens, effort=medium 334 — 2.2x the cost for the same
        answer quality on a task this well-specified.

        It is also a correctness lever. Reasoning tokens count toward
        `max_tokens`, so an unbounded-reasoning call can exhaust the budget
        before it closes its JSON object and fail with `json_validate_failed`.
        """
        model = self.settings.model_for(tier)
        started = time.perf_counter()

        if self.mock:
            text = (
                json.dumps(mock_payload)
                if isinstance(mock_payload, dict)
                else (mock_payload or "")
            )
            return LLMResponse(
                text=text,
                model=f"{model} (mock)",
                tier=tier,
                latency_ms=(time.perf_counter() - started) * 1000,
                mocked=True,
            )

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if reasoning_effort and _supports_reasoning_effort(model):
            # Passed via extra_body rather than as a named argument: the groq
            # SDK pinned in requirements.txt does not type `reasoning_effort`,
            # but the API accepts it. extra_body works across SDK versions.
            kwargs["extra_body"] = {"reasoning_effort": reasoning_effort}

        assert self._client is not None
        resp = self._call_with_retry(kwargs, json_mode=json_mode)
        latency = (time.perf_counter() - started) * 1000

        usage = getattr(resp, "usage", None)
        p_tok = int(getattr(usage, "prompt_tokens", 0) or 0)
        c_tok = int(getattr(usage, "completion_tokens", 0) or 0)

        return LLMResponse(
            text=(resp.choices[0].message.content or "").strip(),
            model=model,
            tier=tier,
            prompt_tokens=p_tok,
            completion_tokens=c_tok,
            latency_ms=latency,
            usd=price_tokens(model, p_tok, c_tok),
        )

    def _call_with_retry(self, kwargs: dict[str, Any], *, json_mode: bool) -> Any:
        """Send the request, recovering from the one failure mode that actually
        occurs in practice.

        `json_validate_failed` means the model ran out of budget mid-object
        rather than that the prompt is wrong. Doubling the ceiling and pinning
        reasoning to `low` fixes it; dropping strict JSON mode on a final
        attempt lets `LLMResponse.json()` salvage the object from loose text.
        A live call degrades to a slower answer, never to a crash.
        """
        from groq import BadRequestError

        assert self._client is not None
        try:
            return self._client.chat.completions.create(**kwargs)
        except BadRequestError as e:
            if "json_validate_failed" not in str(e):
                raise
            retry = dict(kwargs)
            retry["max_tokens"] = min(int(kwargs.get("max_tokens", 900)) * 2, 4096)
            if _supports_reasoning_effort(str(kwargs.get("model", ""))):
                retry["reasoning_effort"] = "low"
            try:
                return self._client.chat.completions.create(**retry)
            except BadRequestError:
                retry.pop("response_format", None)
                return self._client.chat.completions.create(**retry)

    # -- prompt-guard ----------------------------------------------------

    def guard_score(self, text: str, *, mock_score: float = 0.0) -> LLMResponse:
        """Score an utterance for prompt-injection intent.

        `llama-prompt-guard-2-86m` is a classifier, not a chat model: it returns
        a bare probability string as the message content (verified: 0.9995 on an
        injection attempt, 0.0004 on an ordinary product question) and bills
        essentially zero completion tokens. An 86M-parameter model doing this job
        instead of a general LLM is the cheapest decision in the pipeline.
        """
        tier = ModelTier.TINY
        model = self.settings.model_for(tier)
        started = time.perf_counter()

        if self.mock:
            return LLMResponse(
                text=str(mock_score),
                model=f"{model} (mock)",
                tier=tier,
                latency_ms=(time.perf_counter() - started) * 1000,
                mocked=True,
            )

        assert self._client is not None
        resp = self._client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": text}]
        )
        latency = (time.perf_counter() - started) * 1000
        usage = getattr(resp, "usage", None)
        p_tok = int(getattr(usage, "prompt_tokens", 0) or 0)
        c_tok = int(getattr(usage, "completion_tokens", 0) or 0)

        return LLMResponse(
            text=(resp.choices[0].message.content or "0").strip(),
            model=model,
            tier=tier,
            prompt_tokens=p_tok,
            completion_tokens=c_tok,
            latency_ms=latency,
            usd=price_tokens(model, p_tok, c_tok),
        )

    # -- speech ----------------------------------------------------------

    def transcribe_bytes(
        self, audio: bytes, filename: str = "audio.webm", *, language: str = "en"
    ) -> tuple[str, float]:
        """Whisper on Groq. Returns (text, seconds_of_audio).

        Takes bytes rather than a path so the live-mic WebSocket can transcribe
        an utterance straight from the socket without a temp-file round trip.
        Groq accepts flac/mp3/mp4/m4a/mpeg/mpga/ogg/wav/webm; the browser's
        MediaRecorder produces `audio/webm;codecs=opus`, which is on that list.
        """
        if self.mock:
            return ("[mock transcription]", 0.0)
        assert self._client is not None
        result = self._client.audio.transcriptions.create(
            file=(filename, audio),
            model=self.settings.model_stt,
            response_format="verbose_json",
            language=language,
            prompt=STT_PROMPT,
        )
        text = getattr(result, "text", "") or ""
        duration = float(getattr(result, "duration", 0.0) or 0.0)
        return normalise_currency(text.strip()), duration

    def transcribe(self, file_path: str) -> tuple[str, float]:
        """Path-based convenience wrapper over `transcribe_bytes`."""
        if self.mock:
            return ("[mock transcription]", 0.0)
        with open(file_path, "rb") as fh:
            return self.transcribe_bytes(fh.read(), filename=file_path)


_client: Optional[LLMClient] = None


def get_llm() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client

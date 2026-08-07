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

    def transcribe(self, file_path: str) -> tuple[str, float]:
        """Whisper on Groq. Returns (text, seconds_of_audio)."""
        if self.mock:
            return ("[mock transcription]", 0.0)
        assert self._client is not None
        with open(file_path, "rb") as fh:
            result = self._client.audio.transcriptions.create(
                file=(file_path, fh.read()),
                model=self.settings.model_stt,
                response_format="verbose_json",
            )
        text = getattr(result, "text", "") or ""
        duration = float(getattr(result, "duration", 0.0) or 0.0)
        return text, duration


_client: Optional[LLMClient] = None


def get_llm() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client

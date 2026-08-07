"""
Provider failures must reach the agent as a sentence, not a 500.

The Groq daily token allowance ran out mid-call and the dashboard displayed
"500 Internal Server Error" while a customer was on the line. Nothing on screen
said whether the fault was the call, the customer, the account, or the code.
"""

from __future__ import annotations

from app.llm.client import LLMUnavailable

RATE_LIMIT_DAILY = (
    "Error code: 429 - {'error': {'message': 'Rate limit reached for model "
    "`openai/gpt-oss-120b` in organization `org_01k8` service tier `on_demand` "
    "on tokens per day (TPD): Limit 200000, Used 198302, Requested 2226. "
    "Please try again in 3m48.096s.', 'type': 'tokens', "
    "'code': 'rate_limit_exceeded'}}"
)


class RateLimitError(Exception):
    pass


class APIConnectionError(Exception):
    pass


class AuthenticationError(Exception):
    pass


def test_daily_quota_says_so_plainly():
    e = LLMUnavailable.from_exception(RateLimitError(RATE_LIMIT_DAILY))
    assert e.kind == "rate_limited"
    assert "daily token allowance" in e.human
    # A daily cap must not promise relief in four minutes.
    assert "Try again in" not in e.human


def test_the_retry_window_is_parsed_for_the_header():
    e = LLMUnavailable.from_exception(RateLimitError(RATE_LIMIT_DAILY))
    assert e.retry_after_s is not None
    assert 228 - 1 <= e.retry_after_s <= 228 + 1  # 3m48.096s


def test_a_short_rate_limit_does_offer_a_time():
    short = "Error code: 429 rate_limit_exceeded. Please try again in 12.5s."
    e = LLMUnavailable.from_exception(RateLimitError(short))
    assert e.kind == "rate_limited"
    assert "Try again in" in e.human


def test_network_and_auth_are_distinguished():
    net = LLMUnavailable.from_exception(APIConnectionError("could not connect"))
    assert net.kind == "unreachable" and "network" in net.human

    auth = LLMUnavailable.from_exception(AuthenticationError("Error code: 401"))
    assert auth.kind == "auth" and "GROQ_API_KEY" in auth.human


def test_an_unknown_failure_still_produces_a_usable_message():
    e = LLMUnavailable.from_exception(ValueError("something odd"))
    assert e.kind == "provider_error"
    assert e.human  # never empty -- an empty banner is the original bug
    assert "something odd" in e.detail

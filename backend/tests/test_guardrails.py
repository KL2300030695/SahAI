"""
Tests for the deterministic guardrails.

These are the checks the pitch claims cannot be prompt-injected away, so they
are the ones worth proving. Every test here runs without a network call.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.agents.nba import _trim_to_sentence
from app.agents.crm import _coerce_patch_value, _DROP, detect_opt_out
from app.guardrails import rules
from app.guardrails.pii import redact, scan
from app.llm.client import (
    is_probably_hallucination,
    is_prompt_echo,
    normalise_currency,
)
from app.llm.router import RouteContext, route_nba, mentions_credit_terms
from app.schemas import (
    ActionType,
    Citation,
    ConversionProbability,
    FollowUpTiming,
    Intent,
    InterestLevel,
    ModelTier,
    Sentiment,
    Severity,
)


def cite(chunk_id: str, text: str, effective_to: str | None = None) -> Citation:
    return Citation(
        doc_id=chunk_id.split("#")[0],
        title="t",
        chunk_id=chunk_id,
        text=text,
        score=1.0,
        effective_to=effective_to,
    )


# ---------------------------------------------------------------------------
# grounding
# ---------------------------------------------------------------------------


class TestGrounding:
    def test_figure_present_in_cited_chunk_passes(self):
        c = [cite("pricing#0", "The late payment fee is ₹250 flat per instalment.")]
        r = rules.check_grounding("There's a ₹250 late fee if you miss one.", ["pricing#0"], c)
        assert r.passed
        assert r.enforced_by == "code"

    def test_invented_figure_is_blocked(self):
        c = [cite("pricing#0", "The late payment fee is ₹250 flat.")]
        r = rules.check_grounding("The late fee is ₹499.", ["pricing#0"], c)
        assert not r.passed
        assert r.severity == Severity.BLOCK
        assert "499" in r.detail

    def test_figure_with_no_citation_at_all_is_blocked(self):
        r = rules.check_grounding("KYC takes about 5 minutes.", [], [])
        assert not r.passed
        assert "cites no knowledge-base chunk" in r.detail

    def test_no_figures_means_nothing_to_ground(self):
        r = rules.check_grounding("Let me check that for you.", [], [])
        assert r.passed

    def test_the_real_regression_kyc_duration(self):
        """The live run produced 'about 5 minutes' when the KB says 'under 4'.
        This is that exact case."""
        c = [cite("kyc#0", "Typical completion time: under 4 minutes end to end.")]
        r = rules.check_grounding("It's all done in about 5 minutes.", ["kyc#0"], c)
        assert not r.passed

    def test_hallucinated_chunk_id_cannot_satisfy_the_check(self):
        """Citing an id that was never retrieved must not launder a claim."""
        c = [cite("pricing#0", "Late fee is ₹250.")]
        r = rules.check_grounding("The fee is ₹999.", ["made-up#7"], c)
        assert not r.passed


# ---------------------------------------------------------------------------
# staleness
# ---------------------------------------------------------------------------


class TestStaleTerms:
    def test_citing_an_expired_chunk_is_blocked(self):
        c = [cite("old#0", "Processing fee ₹199.", effective_to="2026-03-31")]
        r = rules.check_stale_terms(c, ["old#0"], on=date(2026, 8, 7))
        assert not r.passed
        assert r.severity == Severity.BLOCK

    def test_current_chunk_passes(self):
        c = [cite("new#0", "Processing fee ₹0.")]
        r = rules.check_stale_terms(c, ["new#0"], on=date(2026, 8, 7))
        assert r.passed

    def test_reports_what_the_retriever_filtered(self):
        r = rules.check_stale_terms([], [], dropped_stale=["old#1", "old#2"])
        assert r.passed
        assert "2 expired chunk" in r.detail


# ---------------------------------------------------------------------------
# human oversight on credit terms
# ---------------------------------------------------------------------------


class TestCreditTerms:
    @pytest.mark.parametrize(
        "text",
        [
            "Your credit limit will be around ₹50,000.",
            "You're eligible for this.",
            "The interest rate is zero.",
            "There's a processing fee.",
            "You'll be approved.",
        ],
    )
    def test_credit_terminology_forces_confirmation(self, text):
        _, forced = rules.check_credit_terms(text, ActionType.EXPLAIN, False)
        assert forced is True, "code must force the flag on regardless of the model"

    def test_quote_terms_action_forces_confirmation(self):
        _, forced = rules.check_credit_terms(
            "Here are the numbers.", ActionType.QUOTE_TERMS, False
        )
        assert forced is True

    def test_model_cannot_lower_a_flag_it_set(self):
        """The model saying False on credit content must not win."""
        result, forced = rules.check_credit_terms(
            "Your credit limit is ₹50,000.", ActionType.EXPLAIN, False
        )
        assert forced is True
        assert "FORCED" in result.detail

    def test_benign_text_is_left_alone(self):
        _, forced = rules.check_credit_terms(
            "I'll stay on the line while you do it.", ActionType.EXPLAIN, False
        )
        assert forced is False


# ---------------------------------------------------------------------------
# PII
# ---------------------------------------------------------------------------


class TestPII:
    @pytest.mark.parametrize(
        "text,kind",
        [
            ("My number is 98450 33127", "phone"),
            ("PAN is ABCDE1234F", "pan"),
            ("Aadhaar 1234 5678 9012", "aadhaar"),
            ("write to me at a.b+x@example.co.in", "email"),
            ("the OTP is 483920", "otp"),
            ("card 4111 1111 1111 1111", "card"),
        ],
    )
    def test_each_pii_kind_is_detected_and_masked(self, text, kind):
        r = redact(text)
        assert kind in r.found, f"{kind} not detected in {text!r}"
        assert not scan(r.text), f"PII survived redaction: {r.text!r}"

    def test_clean_text_is_untouched(self):
        r = redact("There's a ₹250 late fee if an instalment is missed.")
        assert not r.changed
        assert "₹250" in r.text

    def test_redaction_is_idempotent(self):
        once = redact("call me on 98450 33127").text
        assert redact(once).text == once

    def test_amounts_are_not_mistaken_for_phone_numbers(self):
        """₹60,000 and ₹1,50,000 must survive — masking real product figures
        would break the grounding check for no benefit."""
        r = redact("Limits run from ₹5,000 to ₹1,50,000, max cart ₹60,000.")
        assert "5,000" in r.text and "60,000" in r.text


# ---------------------------------------------------------------------------
# consent
# ---------------------------------------------------------------------------


class TestConsent:
    def test_missing_consent_is_blocking(self):
        r = rules.check_consent(False)
        assert not r.passed
        assert r.severity == Severity.BLOCK
        assert r.enforced_by == "code"

    def test_consent_present_passes(self):
        assert rules.check_consent(True).passed


# ---------------------------------------------------------------------------
# opt-out
# ---------------------------------------------------------------------------


class TestOptOut:
    @pytest.mark.parametrize(
        "text",
        [
            "No, I'm not interested.",
            "please don't call me about this again",
            "Do not call me",
            "stop calling",
            "remove me from your list",
        ],
    )
    def test_opt_out_phrasings_are_detected(self, text):
        assert detect_opt_out(text)

    def test_ordinary_hesitation_is_not_an_opt_out(self):
        assert not detect_opt_out("Let me think about it and get back to you.")


# ---------------------------------------------------------------------------
# routing
# ---------------------------------------------------------------------------


class TestRouting:
    def test_routine_turn_stays_cheap(self):
        tier, trigger = route_nba(
            RouteContext(intent=Intent.KYC_STEPS, confidence=0.9, dropoff_risk=0.1,
                         text="what documents do I need")
        )
        assert tier == ModelTier.STANDARD
        assert trigger is None

    def test_sensitive_intent_escalates(self):
        tier, trigger = route_nba(
            RouteContext(intent=Intent.ELIGIBILITY, confidence=0.9, dropoff_risk=0.1,
                         text="will I qualify")
        )
        assert tier == ModelTier.HIGH
        assert "sensitive_intent" in trigger

    def test_high_dropoff_escalates(self):
        tier, trigger = route_nba(
            RouteContext(intent=Intent.SMALLTALK, confidence=0.9, dropoff_risk=0.85,
                         text="let me think about it")
        )
        assert tier == ModelTier.HIGH
        assert "high_dropoff_risk" in trigger

    def test_low_confidence_escalates(self):
        tier, _ = route_nba(
            RouteContext(intent=Intent.OTHER, confidence=0.2, dropoff_risk=0.0, text="hm")
        )
        assert tier == ModelTier.HIGH

    def test_kb_style_prose_does_not_escalate_on_its_own(self):
        """Regression: routing once read retrieved KB text, so every turn hit the
        120B model because the KB is *about* fees and eligibility. Routing must
        key on the customer's words only."""
        tier, trigger = route_nba(
            RouteContext(intent=Intent.SMALLTALK, confidence=0.95, dropoff_risk=0.05,
                         text="okay, thanks")
        )
        assert tier == ModelTier.STANDARD, trigger

    def test_credit_terminology_detection(self):
        assert mentions_credit_terms("what's the interest rate")
        assert not mentions_credit_terms("what time do you close")

    @pytest.mark.parametrize("intent", [Intent.COMPLAINT, Intent.PAYMENT_ISSUE])
    def test_service_calls_escalate(self, intent):
        """A caller with a problem is the easiest person to lose and the hardest
        turn to get right -- the correct move is usually to stop selling and
        route them, which is exactly what a cheap model gets wrong."""
        tier, trigger = route_nba(
            RouteContext(intent=intent, confidence=0.95, dropoff_risk=0.2,
                         text="the AC I bought yesterday is faulty")
        )
        assert tier == ModelTier.HIGH
        assert "sensitive_intent" in trigger

    @pytest.mark.parametrize("mood", [Sentiment.ANGRY, Sentiment.FRUSTRATED])
    def test_negative_sentiment_escalates(self, mood):
        tier, trigger = route_nba(
            RouteContext(intent=Intent.KYC_STEPS, confidence=0.95,
                         dropoff_risk=0.1, text="I have called three times",
                         sentiment=mood)
        )
        assert tier == ModelTier.HIGH
        assert "negative_sentiment" in trigger

    def test_calm_customer_on_a_routine_intent_stays_cheap(self):
        """The sentiment rule must not quietly escalate every turn."""
        tier, _ = route_nba(
            RouteContext(intent=Intent.KYC_STEPS, confidence=0.95,
                         dropoff_risk=0.1, text="what documents do I need",
                         sentiment=Sentiment.INTERESTED)
        )
        assert tier == ModelTier.STANDARD


# ---------------------------------------------------------------------------
# CRM patch coercion
# ---------------------------------------------------------------------------


class TestFabricatedActions:
    """The assistant has no side effects. Claiming otherwise makes the human
    agent tell the customer something false."""

    def test_the_real_regression_claimed_sending_an_email(self):
        """Observed live: 'Sure Arun, I've just sent you an email with all the
        Pay-in-3 details and your account info. Please check your inbox.'"""
        r = rules.check_no_fabricated_actions(
            "Sure Arun, I've just sent you an email with all the Pay-in-3 "
            "details. Please check your inbox."
        )
        assert not r.passed
        assert r.severity == Severity.BLOCK
        assert r.enforced_by == "code"

    @pytest.mark.parametrize(
        "text",
        [
            "I've sent you the details.",
            "I have emailed the fee breakdown.",
            "I've updated your account.",
            "Your application has been submitted.",
            "Check your inbox for the link.",
            "It's on its way to you now.",
            "I just sent you an SMS.",
            # Found by watching a live run — record-keeping verbs are as much a
            # false promise as "sent", because the CRM patch is still pending.
            "I've marked you as do-not-call.",
            "I have noted your preference.",
            "I've recorded that for you.",
            "We've updated your account.",
            "Your details have been recorded.",
            "You're all set.",
            "I've opted you out.",
        ],
    )
    def test_completed_action_claims_are_blocked(self, text):
        assert not rules.check_no_fabricated_actions(text).passed

    @pytest.mark.parametrize(
        "text",
        [
            "I'll send that across once we're done.",
            "I can email the fee breakdown if that helps.",
            "You'll see your limit in the app once KYC is complete.",
            "There's a ₹250 late fee if an instalment is missed.",
            "Would it help if I sent the details in writing?",
            "I'll note that and we won't reach out further.",
            "I'll mark you as do-not-call before I close this.",
            "Let me record that for you now.",
        ],
    )
    def test_future_and_offered_actions_are_fine(self, text):
        assert rules.check_no_fabricated_actions(text).passed


class TestHallucinationFilter:
    """Whisper answers silence with training priors, not with nothing.

    Anything accepted here enters conversation history and is fed to the intent
    classifier on every later turn, so filler is not a cosmetic problem.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "Thank you.",
            "thank you very much",
            "Thanks for watching!",
            "Please subscribe",
            "Bye.",
            "You",
            "I'm not sure.",
            "Spoken up.",
            "[MUSIC]",
            "(upbeat music)",
            "...",
            "",
            "  ",
        ],
    )
    def test_known_filler_is_dropped(self, text):
        assert is_probably_hallucination(text)

    @pytest.mark.parametrize(
        "text",
        [
            "I have stuck with the KYC updation.",
            "What's the catch, where are you actually making money?",
            "Thank you, that's much clearer than I expected.",
            "No, I'm not interested. Please don't call me again.",
            "Is there a processing fee?",
        ],
    )
    def test_real_utterances_survive(self, text):
        assert not is_probably_hallucination(text)

    def test_thank_you_inside_a_real_sentence_survives(self):
        """Only a bare filler phrase is dropped — substring matching here would
        silently delete genuine customer turns."""
        assert not is_probably_hallucination("Thank you, that answers it.")

    def test_long_transcript_from_a_sub_second_clip_is_rejected(self):
        assert is_probably_hallucination(
            "Thanks for watching and please subscribe to the channel", 0.4
        )

    def test_short_clip_with_short_plausible_text_is_kept(self):
        assert not is_probably_hallucination("No thanks", 0.5)


class TestPromptEcho:
    """Whisper transcribing our own priming prompt back as customer speech.

    Worse than an ordinary mis-transcription: a fragment of the system's own
    configuration enters conversation history as something the customer said,
    and is then fed to the intent classifier on every later turn.
    """

    def test_the_observed_leak(self):
        """Seen live in the transcript pane, for a turn where nobody spoke.
        Paraphrased -- STT_PROMPT says "Spoken numbers like 'one ninety nine'"
        and Whisper rendered it "the U.S." -- so exact matching cannot catch it."""
        assert is_prompt_echo("Spoken numbers like the U.S.")
        assert is_probably_hallucination("Spoken numbers like the U.S.")

    @pytest.mark.parametrize(
        "text",
        [
            "Spoken numbers like one ninety nine mean 199",
            "Indian fintech inside-sales call about PayFlex Pay-in-3",
            "All money amounts are in Indian rupees",
        ],
    )
    def test_other_prompt_fragments_are_caught(self, text):
        assert is_prompt_echo(text)

    @pytest.mark.parametrize(
        "text",
        [
            "My friend told me there is a 199 processing fee",
            "I am calling you regarding the issue with the AC I bought yesterday",
            "Sir, can you please clarify some questions based on the presentation?",
            "Honestly I do not believe the zero cost thing",
            "What happens if I miss a payment, does interest pile up?",
            "No I am not interested, please do not call me again",
            "Can I use this at Croma for a washing machine?",
            "It asked for Aadhaar and I am not comfortable with that",
        ],
    )
    def test_real_customer_speech_is_never_mistaken_for_an_echo(self, text):
        """A false positive here silently deletes a real customer turn, which is
        worse than letting an echo through."""
        assert not is_prompt_echo(text)

    def test_a_caller_saying_the_amount_is_untouched(self):
        """'one ninety nine' appears in the prompt, but a caller saying it shares
        no trigram with it."""
        assert not is_prompt_echo("It was one ninety nine rupees I think")

    def test_very_short_text_is_left_to_the_filler_filter(self):
        assert not is_prompt_echo("ok sure")


class TestSpeechNormalisation:
    """Whisper mangles spoken Indian rupee amounts in two specific ways.

    Both matter because the grounding guardrail matches figures against
    retrieved chunk text — a mis-transcribed amount either matches nothing (a
    wasted turn) or, worse, matches the wrong thing.
    """

    def test_dollar_decimal_becomes_rupee_hundreds(self):
        """The measured failure: 'one ninety nine' transcribed as '$1.99'."""
        assert normalise_currency("a $1.99 processing fee") == "a ₹199 processing fee"

    def test_plain_dollar_amount_becomes_rupees(self):
        assert normalise_currency("$250 late fee") == "₹250 late fee"

    def test_genuine_decimal_is_preserved(self):
        """Two-digit integer part reads as a real amount, not a misheard one."""
        assert normalise_currency("$12.50 charge") == "₹12.50 charge"

    def test_hyphenated_spoken_number_is_joined(self):
        """The other measured failure: 'one ninety nine' as '1-99'."""
        assert normalise_currency("a 1-99 processing fee") == "a 199 processing fee"
        assert normalise_currency("2-50 late fee") == "250 late fee"

    def test_indian_digit_grouping_survives(self):
        assert normalise_currency("$1,50,000 limit") == "₹1,50,000 limit"

    def test_text_without_currency_is_untouched(self):
        text = "I don't want another credit product on my name."
        assert normalise_currency(text) == text

    def test_dates_and_versions_are_not_mangled(self):
        """The hyphen rule is scoped to 1-2 digits + exactly 2, so real ranges
        and version strings must pass through."""
        assert normalise_currency("v1-234 build") == "v1-234 build"
        assert normalise_currency("2024-2026 period") == "2024-2026 period"


class TestCRMNoteCoercion:
    """The structured note fields must degrade safely — a model returning an
    unknown label should not raise mid-call, and an opt-out must override
    whatever the model concluded about prospects."""

    def _coerce(self, data, **kw):
        from app.agents.crm import _coerce

        return _coerce(data, opted_out=kw.get("opted_out", False),
                       do_not_call=kw.get("do_not_call", False))

    def test_structured_fields_survive(self):
        out = self._coerce({
            "summary": "s", "disposition": "dropped",
            "questions_asked": ["Is there a fee?", "How long is KYC?"],
            "objections": ["Wants a limit first"],
            "interest_level": "warm", "conversion_probability": "medium",
            "conversion_rationale": "Started KYC then stalled on privacy.",
            "followup_timing": "within_2_hours", "sentiment": "hesitant",
            # A timing only survives alongside a draft — see the test below.
            "followup_channel": "sms",
            "followup_body": "Your progress is saved for 7 days: {link}",
        })
        assert out.interest_level is InterestLevel.WARM
        assert out.conversion_probability is ConversionProbability.MEDIUM
        assert out.followup_timing is FollowUpTiming.IMMEDIATE
        assert out.sentiment is Sentiment.HESITANT
        assert len(out.questions_asked) == 2 and len(out.objections) == 1

    def test_unknown_labels_fall_back_rather_than_raise(self):
        out = self._coerce({
            "disposition": "dropped", "interest_level": "scorching",
            "conversion_probability": "certain", "followup_timing": "next tuesday",
            "sentiment": "vibing",
        })
        assert out.interest_level is InterestLevel.COLD
        assert out.conversion_probability is ConversionProbability.LOW
        assert out.followup_timing is FollowUpTiming.NONE
        assert out.sentiment is Sentiment.NEUTRAL

    def test_opt_out_overrides_optimistic_scoring(self):
        """The model may still report a hot prospect on a call that ended in an
        opt-out. Code wins."""
        out = self._coerce({
            "disposition": "dropped", "interest_level": "hot",
            "conversion_probability": "high", "followup_timing": "tomorrow_morning",
            "followup_channel": "sms", "followup_body": "come back!",
        }, opted_out=True)
        assert out.interest_level is InterestLevel.COLD
        assert out.conversion_probability is ConversionProbability.LOW
        assert out.followup_timing is FollowUpTiming.NONE
        assert out.followup_draft is None

    def test_no_draft_means_no_timing(self):
        """A suppressed follow-up must not carry a timing implying one is coming."""
        out = self._coerce({
            "disposition": "converted", "followup_timing": "tomorrow_morning",
            "followup_channel": "none",
        })
        assert out.followup_draft is None
        assert out.followup_timing is FollowUpTiming.NONE

    def test_pii_in_structured_fields_is_redacted(self):
        out = self._coerce({
            "disposition": "dropped",
            "questions_asked": ["Can you text me on 98450 33127?"],
        })
        assert "98450" not in out.questions_asked[0]


class TestPatchCoercion:
    def test_named_kyc_step_becomes_an_integer(self):
        """The live run returned 'aadhaar_verified' for an integer column."""
        assert _coerce_patch_value("kyc_last_step", "aadhaar_verified") == 3

    def test_numeric_string_becomes_an_integer(self):
        assert _coerce_patch_value("kyc_last_step", "step 4") == 4

    def test_uncoercible_value_is_dropped_not_written(self):
        assert _coerce_patch_value("kyc_last_step", "somewhere in the middle") is _DROP

    def test_bool_strings_coerce(self):
        assert _coerce_patch_value("do_not_call", "true") is True
        assert _coerce_patch_value("do_not_call", "no") is False

    def test_correct_types_pass_through(self):
        assert _coerce_patch_value("kyc_status", "completed") == "completed"
        assert _coerce_patch_value("kyc_last_step", 5) == 5


# ---------------------------------------------------------------------------
# Trimming a runaway or cut-off suggestion
#
# The suggestion is now read aloud, so an unfinished sentence is not just untidy
# — the agent hears the co-pilot stop mid-clause and has to guess the rest.
# ---------------------------------------------------------------------------


def test_trim_drops_a_cut_off_fragment():
    """The real failure: a response truncated mid-sentence by max_tokens."""
    say = (
        "First we verify your mobile with an OTP. Then you enter your PAN. "
        "After"
    )
    assert _trim_to_sentence(say) == (
        "First we verify your mobile with an OTP. Then you enter your PAN."
    )


def test_trim_leaves_a_complete_short_line_untouched():
    say = "That's a fair question — let me check the exact figure for you."
    assert _trim_to_sentence(say) == say


def test_trim_never_cuts_mid_clause_when_there_is_no_boundary():
    """A fragment with no earlier sentence to fall back on is left alone.

    Cutting here would be worse than leaving it: "we never share your Aadhaar"
    truncated to its first four words asserts the opposite of what was written.
    """
    say = "We never upload or share your Aadhaar number with"
    assert _trim_to_sentence(say) == say


def test_trim_shortens_a_line_that_blows_the_word_budget():
    sentence = "This is a padded sentence used to exceed the spoken word budget. "
    say = (sentence * 6).strip()
    out = _trim_to_sentence(say)
    assert len(out.split()) <= 80
    assert out.endswith(".")
    assert say.startswith(out)  # only ever removes from the end


def test_trim_preserves_a_negation_it_cannot_safely_shorten():
    """Over budget, but every boundary sits after the negation — keep it whole."""
    say = (
        "We never upload or share your Aadhaar number with anyone outside the "
        "e-KYC flow, and the one-time password you receive is the only thing "
        "that authorises it, which is why the whole verification takes under a "
        "minute from start to finish and needs no paperwork from your side."
    )
    out = _trim_to_sentence(say)
    assert "never upload or share" in out


# ---------------------------------------------------------------------------
# Post-call grounding is a different question from live-turn grounding
#
# A live suggestion must be traceable to the handbook. A summary must be
# traceable to the call. Asking the first question of a summary failed every
# summary that mentioned a number, because post-call retrieval passes no
# citations -- and the dashboard then reported a stopped check above a call that
# had been signed off and written.
# ---------------------------------------------------------------------------

_TRANSCRIPT = (
    "Customer: is there any hidden charge? Agent: no, it is zero cost. There is "
    "a Rs 199 late fee if you miss a payment. Customer: fine, three months works."
)


def test_a_summary_may_quote_figures_from_the_call():
    r = rules.check_figures_in_source(
        "Arun asked about hidden charges. Confirmed the Rs 199 late fee.", _TRANSCRIPT
    )
    assert r.passed, r.detail


def test_a_summary_with_no_figures_passes():
    assert rules.check_figures_in_source("Customer converted after KYC.", _TRANSCRIPT).passed


def test_a_summary_cannot_invent_a_figure_nobody_said():
    """The failure actually worth catching: a number written into a record."""
    r = rules.check_figures_in_source(
        "Agreed to a Rs 5,000 limit for the customer.", _TRANSCRIPT
    )
    assert not r.passed
    assert "5,000" in r.detail


def test_digits_are_not_flattened_across_the_whole_transcript():
    """"Rs 1" and "Rs 99" in the source must not satisfy a claim of "Rs 199".

    Searching one concatenated digit string would let an invented figure through
    whenever its digits happened to appear consecutively somewhere in the call,
    which is the single thing this check exists to stop.
    """
    r = rules.check_figures_in_source("A Rs 199 charge applies.", "We discussed Rs 1 and Rs 99.")
    assert not r.passed


def test_the_old_kb_check_would_have_failed_all_of_these():
    """Pins the bug, so nobody re-points post-call at the knowledge base.

    check_grounding with no citations -- exactly what post-call passed -- fails
    any text containing a figure, however ordinary.
    """
    ordinary = "Arun asked about hidden charges. Confirmed the Rs 199 late fee."
    assert not rules.check_grounding(ordinary, [], []).passed
    assert rules.check_figures_in_source(ordinary, _TRANSCRIPT).passed

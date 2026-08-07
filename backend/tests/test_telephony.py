"""
Tests for the telephony layer.

The codec and the VAD are the parts with no LLM in them and no room for
"roughly right" — a mu-law table that is off by one produces audio that sounds
almost fine and transcribes badly, which is exactly the sort of bug that is
invisible until a demo.
"""

from __future__ import annotations

import math
import struct
import wave
import io

import pytest

from app.schemas import Speaker
from app.telephony.audio import (
    SAMPLE_RATE,
    UtteranceBuffer,
    pcm16_to_ulaw,
    pcm16_to_wav,
    rms,
    ulaw_to_pcm16,
)
from app.telephony.twilio import (
    TRACK_TO_SPEAKER,
    build_twiml,
    parse_message,
    verify_signature,
)


def tone(samples: int = 800, amplitude: int = 12000) -> bytes:
    return b"".join(
        struct.pack("<h", int(amplitude * math.sin(i / 4))) for i in range(samples)
    )


SILENT_ULAW_FRAME = b"\xff" * 160  # mu-law encodes silence as 0xFF


class TestUlawCodec:
    def test_decode_produces_two_bytes_per_sample(self):
        assert len(ulaw_to_pcm16(b"\x00" * 160)) == 320

    def test_round_trip_preserves_signal_energy(self):
        """mu-law is lossy by design; energy should survive within ~5%."""
        pcm = tone()
        back = ulaw_to_pcm16(pcm16_to_ulaw(pcm))
        assert len(back) == len(pcm)
        assert rms(back) == pytest.approx(rms(pcm), rel=0.05)

    def test_silence_decodes_to_near_zero(self):
        assert rms(ulaw_to_pcm16(SILENT_ULAW_FRAME)) < 0.001

    def test_round_trip_preserves_sign(self):
        """A table error that drops the sign bit still 'works' and sounds wrong."""
        pcm = struct.pack("<h", -8000) + struct.pack("<h", 8000)
        back = ulaw_to_pcm16(pcm16_to_ulaw(pcm))
        a, b = struct.unpack("<hh", back)
        assert a < 0 < b

    def test_decoding_is_total_over_the_byte_range(self):
        out = ulaw_to_pcm16(bytes(range(256)))
        assert len(out) == 512


class TestWavPackaging:
    def test_wav_header_is_8khz_mono_16bit(self):
        data = pcm16_to_wav(tone())
        with wave.open(io.BytesIO(data), "rb") as w:
            assert w.getnchannels() == 1
            assert w.getsampwidth() == 2
            assert w.getframerate() == SAMPLE_RATE

    def test_payload_survives_the_container(self):
        pcm = tone()
        with wave.open(io.BytesIO(pcm16_to_wav(pcm)), "rb") as w:
            assert w.readframes(w.getnframes()) == pcm


class TestUtteranceBuffer:
    def test_silence_alone_never_emits(self):
        """A caller on hold must not produce a clip of nothing for Whisper to
        hallucinate over."""
        buf = UtteranceBuffer()
        for _ in range(300):  # 6 seconds
            assert buf.add(SILENT_ULAW_FRAME) is None
        assert buf.flush() is None

    def test_speech_then_silence_emits_a_wav(self):
        buf = UtteranceBuffer()
        speech = pcm16_to_ulaw(tone())[:160]
        out = None
        for _ in range(30):
            out = buf.add(speech) or out
        for _ in range(60):
            if (r := buf.add(SILENT_ULAW_FRAME)) is not None:
                out = r
                break
        assert out is not None and out[:4] == b"RIFF"

    def test_a_brief_blip_is_discarded(self):
        """Shorter than min_ms — a cough or a line click, not an utterance."""
        buf = UtteranceBuffer(min_ms=400)
        speech = pcm16_to_ulaw(tone())[:160]
        buf.add(speech)  # 20ms only
        assert buf.flush() is None

    def test_a_monologue_is_force_cut(self):
        buf = UtteranceBuffer(max_ms=200)
        speech = pcm16_to_ulaw(tone())[:160]
        out = None
        for _ in range(20):
            out = buf.add(speech) or out
        assert out is not None, "must not buffer forever while someone talks"

    def test_buffer_resets_after_emitting(self):
        buf = UtteranceBuffer(max_ms=200)
        speech = pcm16_to_ulaw(tone())[:160]
        for _ in range(20):
            buf.add(speech)
        assert buf.duration_ms == 0 and not buf.has_speech


class TestTwiML:
    def test_uses_start_not_connect(self):
        """<Connect> hands the call to the socket and expects audio back — that
        is a bot. A co-pilot forks a copy and never speaks to the customer."""
        xml = build_twiml("wss://x/stream", call_id="tel-1")
        assert "<Start>" in xml and "<Connect>" not in xml

    def test_requests_both_legs(self):
        """Both tracks is what makes speaker attribution exact."""
        assert 'track="both_tracks"' in build_twiml("wss://x/s", call_id="tel-1")

    def test_threads_our_call_id_through(self):
        xml = build_twiml("wss://x/s", call_id="tel-abc123")
        assert 'name="call_id" value="tel-abc123"' in xml

    def test_consent_disclosure_precedes_the_stream(self):
        """Consent must be spoken before a single frame is forked."""
        xml = build_twiml("wss://x/s", call_id="t", greeting="may be recorded")
        assert xml.index("<Say") < xml.index("<Start>")

    def test_xml_special_characters_are_escaped(self):
        xml = build_twiml("wss://x/s?a=1&b=2", call_id="t")
        assert "&amp;" in xml and "?a=1&b=2" not in xml


class TestMediaStreamParsing:
    def test_start_carries_our_call_id(self):
        ev = parse_message({
            "event": "start", "streamSid": "MZ1",
            "start": {"callSid": "CA1", "customParameters": {"call_id": "tel-9"}},
        })
        assert ev.kind == "start" and ev.call_id == "tel-9"

    def test_inbound_track_is_the_customer(self):
        import base64
        ev = parse_message({
            "event": "media", "streamSid": "MZ1",
            "media": {"track": "inbound", "chunk": 3,
                      "payload": base64.b64encode(b"\xff" * 160).decode()},
        })
        assert ev.speaker == Speaker.CUSTOMER and len(ev.payload) == 160

    def test_outbound_track_is_the_agent(self):
        import base64
        ev = parse_message({
            "event": "media",
            "media": {"track": "outbound",
                      "payload": base64.b64encode(b"\xff" * 160).decode()},
        })
        assert ev.speaker == Speaker.AGENT

    def test_malformed_payload_does_not_raise(self):
        """A carrier hiccup must not take down a live call."""
        ev = parse_message({"event": "media", "media": {"payload": "!!not base64!!"}})
        assert ev.kind == "media" and ev.payload == b""

    def test_unknown_event_is_tolerated(self):
        assert parse_message({"event": "somethingNew"}).kind == "unknown"

    def test_track_mapping_covers_both_legs(self):
        assert set(TRACK_TO_SPEAKER) == {"inbound", "outbound"}


class TestSignatureVerification:
    def test_valid_signature_passes(self):
        import base64, hashlib, hmac
        token, url = "secret", "https://x/api/telephony/voice"
        params = {"CallSid": "CA1", "From": "+919845033127"}
        payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
        sig = base64.b64encode(
            hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
        ).decode()
        assert verify_signature(token, url, params, sig)

    def test_forged_signature_is_rejected(self):
        """The webhook is public by necessity — this is what stops a stranger
        opening call sessions on your account."""
        assert not verify_signature(
            "secret", "https://x/v", {"CallSid": "CA1"}, "bm90LXJpZ2h0"
        )

    def test_no_token_configured_skips_verification(self):
        """So the simulator and local development still work."""
        assert verify_signature("", "https://x/v", {"a": "b"}, "")

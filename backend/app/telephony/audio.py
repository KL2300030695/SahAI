"""
Telephony audio: G.711 mu-law decoding, WAV packaging, and energy VAD.

Carriers deliver call audio as 8kHz mono G.711 mu-law in 20ms frames. Whisper
wants a decodable audio file. This module is the bridge, and it is deliberately
pure Python with no third-party codec dependency:

* `audioop` (the stdlib route) is deprecated from 3.11 and **removed in 3.13**,
  so depending on it would quietly break the project on a newer interpreter.
* Pulling in numpy or a native codec for what is a 256-entry lookup table and an
  RMS loop would be a heavy dependency for very little.

The decode table is built once at import; per-frame work is a table lookup.
"""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass, field

SAMPLE_RATE = 8000  # G.711 is always 8kHz
SAMPLE_WIDTH = 2  # PCM16 after decoding
FRAME_MS = 20  # carriers emit 20ms frames
BYTES_PER_FRAME = SAMPLE_RATE * FRAME_MS // 1000  # 160 mu-law bytes


def _build_ulaw_table() -> list[int]:
    """G.711 mu-law -> signed 16-bit PCM, per ITU-T G.711."""
    table = []
    for byte in range(256):
        u = ~byte & 0xFF
        sign = u & 0x80
        exponent = (u >> 4) & 0x07
        mantissa = u & 0x0F
        sample = (((mantissa << 3) + 0x84) << exponent) - 0x84
        table.append(-sample if sign else sample)
    return table


_ULAW_TABLE = _build_ulaw_table()


def ulaw_to_pcm16(payload: bytes) -> bytes:
    """Decode mu-law bytes to little-endian PCM16."""
    out = bytearray(len(payload) * 2)
    for i, byte in enumerate(payload):
        sample = _ULAW_TABLE[byte]
        if sample < 0:
            sample += 0x10000
        out[2 * i] = sample & 0xFF
        out[2 * i + 1] = (sample >> 8) & 0xFF
    return bytes(out)


def pcm16_to_wav(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Wrap raw PCM16 in a WAV container so Whisper can decode it."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buf.getvalue()


def rms(pcm: bytes) -> float:
    """Normalised RMS (0..1) of a PCM16 buffer."""
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    total = 0
    for i in range(0, n * 2, 2):
        sample = pcm[i] | (pcm[i + 1] << 8)
        if sample >= 0x8000:
            sample -= 0x10000
        total += sample * sample
    return (total / n) ** 0.5 / 32768.0


@dataclass
class UtteranceBuffer:
    """Accumulates frames for one speaker and closes on silence.

    Same idea as the browser-side VAD, but server-side because carrier audio
    arrives as a continuous frame stream with no natural boundaries. Closing on
    silence keeps one utterance mapped to one pipeline turn.

    Thresholds are tuned for 8kHz telephony, which is noisier and quieter than a
    laptop microphone -- hence a lower energy threshold than the browser uses.

    `silence_ms` is the setting that matters most, and it is a real trade-off.
    At 700ms a natural sentence pause closed the utterance, so "I don't believe
    the zero-cost thing. My friend said there's a fee. Is that true?" arrived as
    three separate turns -- three transcriptions, three pipeline runs, three
    suggestions, of which only the last answered the actual question. At 900ms
    (matching the browser VAD) it stays one turn. Longer still would add
    noticeable lag before the agent gets help, which defeats the point.
    """

    silence_threshold: float = 0.006
    silence_ms: int = 900
    min_ms: int = 350
    max_ms: int = 15000

    _pcm: bytearray = field(default_factory=bytearray)
    _ms: int = 0
    _silent_ms: int = 0
    _saw_speech: bool = False

    @property
    def duration_ms(self) -> int:
        return self._ms

    @property
    def has_speech(self) -> bool:
        return self._saw_speech

    def add(self, ulaw_payload: bytes) -> bytes | None:
        """Add one frame. Returns a complete WAV when the utterance closes.

        Frames before any speech is detected are discarded rather than buffered,
        so a long silent hold does not accumulate into a giant clip of nothing
        that Whisper will hallucinate over.
        """
        pcm = ulaw_to_pcm16(ulaw_payload)
        frame_ms = int(len(pcm) / 2 / SAMPLE_RATE * 1000) or FRAME_MS
        loud = rms(pcm) > self.silence_threshold

        if not self._saw_speech and not loud:
            return None  # still waiting for someone to speak

        if loud:
            self._saw_speech = True
            self._silent_ms = 0
        else:
            self._silent_ms += frame_ms

        self._pcm.extend(pcm)
        self._ms += frame_ms

        if (
            self._saw_speech
            and self._silent_ms >= self.silence_ms
            and self._ms >= self.min_ms
        ):
            return self.flush()

        # Force-cut a monologue. This path bypasses the min_ms floor: the buffer
        # only got here by accumulating max_ms of continuous speech, so the
        # audio is real by construction. Applying the short-utterance filter
        # here would discard someone who simply has not paused yet.
        if self._ms >= self.max_ms:
            return self.flush(force=True)

        return None

    def flush(self, *, force: bool = False) -> bytes | None:
        """Close the current utterance and return it as WAV, if usable."""
        pcm, ms, speech = bytes(self._pcm), self._ms, self._saw_speech
        self.reset()
        if not pcm or not speech:
            return None
        if not force and ms < self.min_ms:
            return None
        return pcm16_to_wav(pcm)

    def reset(self) -> None:
        self._pcm = bytearray()
        self._ms = 0
        self._silent_ms = 0
        self._saw_speech = False


def pcm16_to_ulaw(pcm: bytes) -> bytes:
    """Encode PCM16 to mu-law. Used by the call simulator."""
    out = bytearray(len(pcm) // 2)
    for i in range(0, len(pcm) - 1, 2):
        sample = pcm[i] | (pcm[i + 1] << 8)
        if sample >= 0x8000:
            sample -= 0x10000
        sign = 0x80 if sample < 0 else 0x00
        sample = min(abs(sample), 32635) + 0x84
        exponent = 7
        for exp in range(7, -1, -1):
            if sample >= (0x84 << exp):
                exponent = exp
                break
        mantissa = (sample >> (exponent + 3)) & 0x0F
        out[i // 2] = ~(sign | (exponent << 4) | mantissa) & 0xFF
    return bytes(out)

"""
Simulate an inbound phone call without a carrier, a phone number, or a tunnel.

Replays a WAV through the real telephony path: it converts the audio to G.711
mu-law, chops it into 20ms frames, and speaks Twilio's Media Streams protocol to
`/ws/telephony/stream` exactly as the carrier would.

Everything downstream is the production path -- same codec handling, same
server-side VAD, same Whisper call, same orchestrator, same guardrails. Only the
source of the frames differs.

That matters for two reasons: telephony can be exercised in CI and on a laptop
with no account, and a live demo has a fallback that does not depend on mobile
signal in a conference hall.

    python simulate_phone_call.py                      # default clip
    python simulate_phone_call.py path/to/audio.wav
    python simulate_phone_call.py clip.wav --realtime  # pace at true 1x
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import wave
from pathlib import Path

import websockets

from app.telephony.audio import FRAME_MS, SAMPLE_RATE, pcm16_to_ulaw

BASE_HTTP = "http://127.0.0.1:8000"
BASE_WS = "ws://127.0.0.1:8000"


def load_pcm8k(path: Path) -> bytes:
    """Read a WAV and return 8kHz mono PCM16, resampling if needed."""
    with wave.open(str(path), "rb") as w:
        channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        frames = w.readframes(w.getnframes())

    if width != 2:
        raise SystemExit(f"{path.name}: need 16-bit PCM, got {width * 8}-bit")

    if channels > 1:  # average channels down to mono
        mono = bytearray()
        step = 2 * channels
        for i in range(0, len(frames) - step + 1, step):
            total = 0
            for c in range(channels):
                s = frames[i + 2 * c] | (frames[i + 2 * c + 1] << 8)
                total += s - 0x10000 if s >= 0x8000 else s
            avg = int(total / channels) & 0xFFFF
            mono += bytes((avg & 0xFF, (avg >> 8) & 0xFF))
        frames = bytes(mono)

    if rate != SAMPLE_RATE:  # nearest-neighbour decimation is fine for 8kHz voice
        ratio = rate / SAMPLE_RATE
        out = bytearray()
        n = len(frames) // 2
        for i in range(int(n / ratio)):
            src = int(i * ratio) * 2
            if src + 1 < len(frames):
                out += frames[src : src + 2]
        frames = bytes(out)

    return frames


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("wav", nargs="?", default="", help="16-bit PCM WAV to replay")
    ap.add_argument("--realtime", action="store_true",
                    help="pace frames at true 1x instead of as fast as possible")
    ap.add_argument("--track", default="inbound", choices=["inbound", "outbound"],
                    help="inbound = customer leg, outbound = agent leg")
    args = ap.parse_args()

    default = Path(__file__).parent / "app" / "seed" / "audio" / "customer_question.wav"
    wav_path = Path(args.wav) if args.wav else default
    if not wav_path.exists():
        print(f"no audio at {wav_path}", file=sys.stderr)
        print("pass a 16-bit PCM WAV path, or generate the sample clip first.",
              file=sys.stderr)
        return 2

    import urllib.request

    # 1. The carrier fetches the TwiML webhook when the call connects.
    body = b"From=%2B919845033127&To=%2B18005550100&CallSid=CAsimulated0001"
    req = urllib.request.Request(
        f"{BASE_HTTP}/api/telephony/voice", data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        twiml = urllib.request.urlopen(req).read().decode()
    except Exception as e:
        print(f"TwiML webhook failed: {e}", file=sys.stderr)
        print("Is PUBLIC_BASE_URL set in .env? The webhook needs it to build a "
              "stream URL.", file=sys.stderr)
        return 1

    call_id = twiml.split('name="call_id" value="')[1].split('"')[0]
    print(f"  TwiML issued          call_id={call_id}")
    print(f"  dashboard can attach  ws /ws/observe/{call_id}")

    pcm = load_pcm8k(wav_path)
    ulaw = pcm16_to_ulaw(pcm)
    frame_bytes = SAMPLE_RATE * FRAME_MS // 1000
    frames = [ulaw[i : i + frame_bytes] for i in range(0, len(ulaw), frame_bytes)]
    print(f"  audio                 {wav_path.name}  "
          f"{len(pcm) / 2 / SAMPLE_RATE:.1f}s  {len(frames)} frames")

    # 2. Speak Media Streams to the socket, as the carrier does.
    async with websockets.connect(f"{BASE_WS}/ws/telephony/stream") as ws:
        await ws.send(json.dumps({"event": "connected", "protocol": "Call"}))
        await ws.send(json.dumps({
            "event": "start", "streamSid": "MZsimulated",
            "start": {"callSid": "CAsimulated0001",
                      "customParameters": {"call_id": call_id}},
        }))

        for i, frame in enumerate(frames):
            await ws.send(json.dumps({
                "event": "media", "streamSid": "MZsimulated",
                "media": {"track": args.track, "chunk": i,
                          "payload": base64.b64encode(frame).decode()},
            }))
            if args.realtime:
                await asyncio.sleep(FRAME_MS / 1000)

        # Trailing silence so the server-side VAD closes the utterance, exactly
        # as it would when the caller stops speaking.
        silence = base64.b64encode(b"\xff" * frame_bytes).decode()
        for i in range(60):  # 1.2s
            await ws.send(json.dumps({
                "event": "media", "streamSid": "MZsimulated",
                "media": {"track": args.track, "chunk": len(frames) + i,
                          "payload": silence},
            }))
            if args.realtime:
                await asyncio.sleep(FRAME_MS / 1000)

        print("  audio sent            waiting for the pipeline…")
        await asyncio.sleep(1)
        await ws.send(json.dumps({"event": "stop", "streamSid": "MZsimulated"}))
        await asyncio.sleep(0.2)

    print(f"\n  call_id: {call_id}")
    print(f"  ledger : {BASE_HTTP}/api/calls/{call_id}/ledger")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

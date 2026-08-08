import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Microphone capture with voice-activity detection.
 *
 * Why VAD rather than a fixed timer: the pipeline is turn-shaped — one customer
 * utterance in, one suggestion out. Slicing audio every N seconds cuts
 * mid-sentence and hands Whisper fragments. Segmenting on silence produces
 * natural utterance boundaries that map 1:1 onto pipeline turns.
 *
 * Why stop/restart the recorder instead of using MediaRecorder's `timeslice`:
 * only the FIRST chunk of a MediaRecorder stream carries the webm header.
 * Later chunks are not independently decodable, so sending them to Whisper
 * yields errors or silence. Stopping the recorder finalises a complete,
 * self-contained file; we then immediately start a new one.
 */

export interface MicState {
  supported: boolean;
  recording: boolean;
  speaking: boolean;
  /** True while audio is being discarded because the co-pilot is talking. */
  muted: boolean;
  level: number; // 0..1 RMS, for the level meter
  error: string | null;
}

interface Options {
  /** Fires once per detected utterance with a complete, decodable audio file. */
  onUtterance: (blob: Blob, ms: number) => void;
  /**
   * Return true while captured audio must be thrown away.
   *
   * This exists for one reason: on a laptop the co-pilot's spoken suggestion
   * comes out of the speakers right next to this microphone. Without a gate the
   * pipeline hears its own voice, transcribes it as the customer, and answers
   * itself — and the transcript fills with fluent sentences nobody said, which
   * is far worse than a missed turn because nothing about it looks wrong.
   */
  gate?: () => boolean;
  /**
   * Fires when the customer starts talking over the co-pilot's voice.
   *
   * The gate alone is not enough. Muting the microphone stops the pipeline
   * hearing itself, but it also means an interruption is thrown away while the
   * co-pilot keeps reading over the person who interrupted — which is exactly
   * backwards on a live call. Barge-in is the other half: detect the
   * interruption, stop reading, and start listening.
   */
  onBargeIn?: () => void;
  /**
   * How much louder than ordinary speech an interruption must be to count
   * while the co-pilot is reading.
   *
   * `getUserMedia` runs with `echoCancellation`, so most of the co-pilot's
   * voice is already removed from the microphone signal — but not all of it on
   * open laptop speakers. A multiple of the normal threshold keeps residual
   * echo from cutting the co-pilot off mid-sentence, which would be far more
   * visible than a missed interruption.
   */
  bargeInFactor?: number;
  /** Sustained loudness required, so a cough or a door does not trigger it. */
  bargeInMs?: number;
  /** RMS below this counts as silence. */
  silenceThreshold?: number;
  /**
   * Silence needed to close an utterance.
   *
   * This is the single most consequential number in the file, and 900ms was
   * too low. People pause mid-sentence — to think, to breathe, to find a word —
   * and a pause longer than this splits one question into several turns.
   * Observed live: "Hello? What if I don't have any..." / "Government ID
   * proof." / "Indian." was one sentence, cut three ways. Whisper then
   * transcribed clipped audio, the intent classifier saw fragments, and the
   * suggestion answered a question nobody had asked — while billing three full
   * pipeline runs for it.
   *
   * The cost of raising it is that the co-pilot answers a few hundred
   * milliseconds later. That is barely perceptible, because the agent is still
   * listening to the customer during it. The cost of leaving it low is a
   * confidently wrong suggestion, which is far worse.
   */
  silenceMs?: number;
  /**
   * Ignore blips shorter than this — coughs, clicks, door slams.
   *
   * Also the second line of defence against fragments: a genuine conversational
   * turn is rarely under half a second, and a clipped one carries no question
   * for the pipeline to answer.
   */
  minUtteranceMs?: number;
  /** Force-close a monologue so the agent still gets help mid-flow. */
  maxUtteranceMs?: number;
}

export function useMic({
  onUtterance,
  gate,
  onBargeIn,
  bargeInFactor = 3,
  bargeInMs = 220,
  silenceThreshold = 0.012,
  silenceMs = 1300,
  minUtteranceMs = 600,
  maxUtteranceMs = 20000,
}: Options) {
  const [state, setState] = useState<MicState>({
    supported:
      typeof navigator !== "undefined" &&
      !!navigator.mediaDevices?.getUserMedia &&
      typeof MediaRecorder !== "undefined",
    recording: false,
    speaking: false,
    muted: false,
    level: 0,
    error: null,
  });

  const streamRef = useRef<MediaStream | null>(null);
  const ctxRef = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<BlobPart[]>([]);
  const rafRef = useRef<number | null>(null);

  const speakingRef = useRef(false);
  const lastVoiceRef = useRef(0);
  const startedAtRef = useRef(0);
  const runningRef = useRef(false);
  const gatedRef = useRef(false);
  const onUtteranceRef = useRef(onUtterance);
  onUtteranceRef.current = onUtterance;
  const gateRef = useRef(gate);
  gateRef.current = gate;
  const onBargeInRef = useRef(onBargeIn);
  onBargeInRef.current = onBargeIn;
  const bargeStartRef = useRef(0);
  /** Set when the customer talked over the reading, so the segment is kept. */
  const bargedRef = useRef(false);

  /** Finalise the current recording; `emit` decides whether anyone hears it. */
  const cutSegment = useCallback((emit: boolean) => {
    const rec = recorderRef.current;
    if (!rec || rec.state === "inactive") return;
    const ms = performance.now() - startedAtRef.current;
    rec.onstop = () => {
      const blob = new Blob(chunksRef.current, {
        type: rec.mimeType || "audio/webm",
      });
      chunksRef.current = [];
      if (emit && blob.size > 2000 && ms >= minUtteranceMs) {
        onUtteranceRef.current(blob, ms);
      }
      // Restart for the next utterance while the session is still running —
      // unless the gate is shut, in which case `tick` restarts us on reopen.
      if (runningRef.current && streamRef.current && !gatedRef.current) {
        startRecorder(streamRef.current);
      } else {
        recorderRef.current = null;
      }
    };
    rec.stop();
  }, [minUtteranceMs]);

  const startRecorder = useCallback((stream: MediaStream) => {
    const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
      ? "audio/webm;codecs=opus"
      : MediaRecorder.isTypeSupported("audio/webm")
        ? "audio/webm"
        : "";
    const rec = mime
      ? new MediaRecorder(stream, { mimeType: mime })
      : new MediaRecorder(stream);
    chunksRef.current = [];
    rec.ondataavailable = (e) => {
      if (e.data.size > 0) chunksRef.current.push(e.data);
    };
    rec.start();
    recorderRef.current = rec;
    startedAtRef.current = performance.now();
  }, []);

  const tick = useCallback(() => {
    const analyser = analyserRef.current;
    if (!analyser || !runningRef.current) return;

    const buf = new Float32Array(analyser.fftSize);
    analyser.getFloatTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
    const rms = Math.sqrt(sum / buf.length);

    const now = performance.now();

    // --- the echo gate -------------------------------------------------
    // While the co-pilot is speaking, throw the audio away rather than merely
    // ignoring the level: a half-recorded segment straddling the end of the
    // suggestion would still carry the tail of it into Whisper.
    const shutNow = !!gateRef.current?.();
    if (shutNow !== gatedRef.current) {
      gatedRef.current = shutNow;
      if (shutNow) {
        // Recording CONTINUES through the reading. It used to be discarded
        // here, which meant an interruption lost its opening words: by the time
        // barge-in fired, a fifth of a second of the customer had already gone
        // unrecorded, and that is usually the part that says what they want.
        //
        // The segment is kept only if they actually interrupt. If the reading
        // finishes undisturbed, whatever the microphone picked up is echo of
        // the co-pilot's own voice and is thrown away below — so the original
        // failure, the pipeline transcribing itself, cannot come back on a turn
        // where nobody spoke.
        speakingRef.current = false;
        bargedRef.current = false;
      } else if (!bargedRef.current) {
        // Read finished with nobody talking over it: bin the echo, start clean.
        cutSegment(false);
        if (streamRef.current && !recorderRef.current) {
          startRecorder(streamRef.current);
        }
      }
      setState((s) => ({ ...s, muted: shutNow, speaking: false, level: 0 }));
    }
    if (shutNow) {
      // Still listening, just not recording. An interruption has to be clearly
      // louder than what leaks back from the speakers, and sustained, before it
      // counts — cutting the co-pilot off mid-word because of its own echo
      // would be a worse failure than missing a barge-in.
      if (rms > silenceThreshold * bargeInFactor) {
        if (!bargeStartRef.current) {
          bargeStartRef.current = now;
        } else if (now - bargeStartRef.current > bargeInMs) {
          bargeStartRef.current = 0;
          // Marked before the callback: the gate can reopen on the very next
          // tick, and the reopen path checks this to decide whether the audio
          // in flight is the customer or the co-pilot's own echo.
          bargedRef.current = true;
          speakingRef.current = true; // their speech is already in the segment
          lastVoiceRef.current = now;
          onBargeInRef.current?.(); // stops the voice; the gate opens next tick
        }
      } else {
        bargeStartRef.current = 0;
      }
      // Keep the silence clock fresh so reopening does not immediately look
      // like the end of a long pause.
      lastVoiceRef.current = now;
      rafRef.current = requestAnimationFrame(tick);
      return;
    }
    bargeStartRef.current = 0;

    const loud = rms > silenceThreshold;
    if (loud) {
      lastVoiceRef.current = now;
      if (!speakingRef.current) speakingRef.current = true;
    }

    const elapsed = now - startedAtRef.current;
    const quietFor = now - lastVoiceRef.current;

    // Close the utterance on a real pause, or when a monologue runs long.
    if (speakingRef.current && quietFor > silenceMs && elapsed > minUtteranceMs) {
      speakingRef.current = false;
      cutSegment(true);
    } else if (elapsed > maxUtteranceMs) {
      speakingRef.current = false;
      cutSegment(true);
    }

    setState((s) =>
      s.level === rms && s.speaking === speakingRef.current
        ? s
        : { ...s, level: rms, speaking: speakingRef.current },
    );

    rafRef.current = requestAnimationFrame(tick);
  }, [
    cutSegment,
    maxUtteranceMs,
    minUtteranceMs,
    silenceMs,
    silenceThreshold,
    startRecorder,
    bargeInFactor,
    bargeInMs,
  ]);

  const start = useCallback(async () => {
    if (runningRef.current) return;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      streamRef.current = stream;

      const ctx = new AudioContext();
      const src = ctx.createMediaStreamSource(stream);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 1024;
      src.connect(analyser);
      ctxRef.current = ctx;
      analyserRef.current = analyser;

      runningRef.current = true;
      lastVoiceRef.current = performance.now();
      speakingRef.current = false;
      gatedRef.current = false;
      bargedRef.current = false;
      startRecorder(stream);

      setState((s) => ({ ...s, recording: true, error: null }));
      rafRef.current = requestAnimationFrame(tick);
    } catch (e: any) {
      setState((s) => ({
        ...s,
        error:
          e?.name === "NotAllowedError"
            ? "Microphone permission denied. Allow it in the browser address bar."
            : String(e?.message ?? e),
      }));
    }
  }, [startRecorder, tick]);

  const stop = useCallback(() => {
    runningRef.current = false;
    if (rafRef.current) cancelAnimationFrame(rafRef.current);
    // Emit whatever was mid-utterance so a final question is not lost.
    cutSegment(true);
    recorderRef.current = null;
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    ctxRef.current?.close().catch(() => {});
    ctxRef.current = null;
    analyserRef.current = null;
    speakingRef.current = false;
    gatedRef.current = false;
    bargedRef.current = false;
    setState((s) => ({
      ...s,
      recording: false,
      speaking: false,
      muted: false,
      level: 0,
    }));
  }, [cutSegment]);

  useEffect(() => () => stop(), [stop]);

  return { ...state, start, stop };
}

// The co-pilot's voice used to live here as a second hook. It moved to
// `lib/speech.ts` as a single shared engine — one `enabled` flag for the whole
// session, and a gate this file consults so the microphone never hears it.

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
  level: number; // 0..1 RMS, for the level meter
  error: string | null;
}

interface Options {
  /** Fires once per detected utterance with a complete, decodable audio file. */
  onUtterance: (blob: Blob, ms: number) => void;
  /** RMS below this counts as silence. */
  silenceThreshold?: number;
  /** Silence needed to close an utterance. */
  silenceMs?: number;
  /** Ignore blips shorter than this — coughs, clicks, door slams. */
  minUtteranceMs?: number;
  /** Force-close a monologue so the agent still gets help mid-flow. */
  maxUtteranceMs?: number;
}

export function useMic({
  onUtterance,
  silenceThreshold = 0.012,
  silenceMs = 900,
  minUtteranceMs = 400,
  maxUtteranceMs = 20000,
}: Options) {
  const [state, setState] = useState<MicState>({
    supported:
      typeof navigator !== "undefined" &&
      !!navigator.mediaDevices?.getUserMedia &&
      typeof MediaRecorder !== "undefined",
    recording: false,
    speaking: false,
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
  const onUtteranceRef = useRef(onUtterance);
  onUtteranceRef.current = onUtterance;

  /** Finalise the current recording; `keepGoing` restarts for the next utterance. */
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
      // Restart for the next utterance while the session is still running.
      if (runningRef.current && streamRef.current) {
        startRecorder(streamRef.current);
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
  }, [cutSegment, maxUtteranceMs, minUtteranceMs, silenceMs, silenceThreshold]);

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
    setState((s) => ({ ...s, recording: false, speaking: false, level: 0 }));
  }, [cutSegment]);

  useEffect(() => () => stop(), [stop]);

  return { ...state, start, stop };
}

/**
 * Speak a suggestion aloud through the browser's speech synthesis.
 *
 * Uses the built-in Web Speech API rather than a TTS provider: it is free,
 * offline, has no added latency, and a co-pilot whispering in the agent's ear
 * does not need a studio voice. Returns a no-op where unsupported.
 */
export function useSpeech() {
  const [enabled, setEnabled] = useState(false);
  const [speaking, setSpeaking] = useState(false);
  const supported =
    typeof window !== "undefined" && "speechSynthesis" in window;

  const speak = useCallback(
    (text: string) => {
      if (!supported || !enabled || !text) return;
      window.speechSynthesis.cancel();
      const u = new SpeechSynthesisUtterance(text);
      u.rate = 1.05;
      u.pitch = 1;
      const voice = window.speechSynthesis
        .getVoices()
        .find((v) => /en-(IN|GB|US)/i.test(v.lang));
      if (voice) u.voice = voice;
      u.onstart = () => setSpeaking(true);
      u.onend = () => setSpeaking(false);
      u.onerror = () => setSpeaking(false);
      window.speechSynthesis.speak(u);
    },
    [enabled, supported],
  );

  const cancel = useCallback(() => {
    if (supported) window.speechSynthesis.cancel();
    setSpeaking(false);
  }, [supported]);

  return { supported, enabled, setEnabled, speaking, speak, cancel };
}

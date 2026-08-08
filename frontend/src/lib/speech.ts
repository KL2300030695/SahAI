import { useCallback, useSyncExternalStore } from "react";

/**
 * The co-pilot's voice — one engine for the whole app.
 *
 * Deliberately a module-level singleton rather than a hook per component. An
 * earlier version called `useSpeech()` inside each voice component
 * separately, which gave each of them its *own* `enabled` flag: switching the
 * voice on in one place left it off everywhere else, and the scripted-call path
 * had no instance at all so it never spoke. A voice is a property of the
 * session, not of a panel.
 *
 * It speaks into the agent's ear, not down the line. The product shape is
 * unchanged — the AI never addresses the customer — this just means the agent
 * can keep their eyes on the customer's words instead of reading a screen.
 *
 * Three browser realities this works around:
 *
 * * `getVoices()` is empty on first call in Chrome and fills in asynchronously,
 *   so the voice preference has to be re-applied on `voiceschanged`.
 * * Chrome silently drops speech that starts before the page has seen a user
 *   gesture, so the engine is primed with a silent utterance on the first one.
 * * Chrome stops mid-sentence after roughly 15 seconds unless the queue is
 *   nudged, hence the pause/resume heartbeat.
 *
 * The important one is not a browser quirk though: see `micMuted`.
 */

/** Silence the microphone for this long after the voice stops. */
const ECHO_TAIL_MS = 400;

/** The same, after an interruption — see `cancel`. */
const BARGE_IN_TAIL_MS = 120;

/** Rough speaking rate, used only to bound how long the mic can stay muted. */
const WORDS_PER_SECOND = 2.6;

const PREFERRED = [/en[-_]IN/i, /en[-_]GB/i, /en[-_]AU/i, /en[-_]US/i, /^en/i];

interface Snapshot {
  enabled: boolean;
  speaking: boolean;
}

class VoiceEngine {
  readonly supported =
    typeof window !== "undefined" &&
    "speechSynthesis" in window &&
    "SpeechSynthesisUtterance" in window;

  private snap: Snapshot = { enabled: false, speaking: false };
  private listeners = new Set<() => void>();
  private voice: SpeechSynthesisVoice | null = null;
  private heartbeat: ReturnType<typeof setInterval> | null = null;
  private primed = false;

  /**
   * Wall-clock time until which the microphone must ignore everything it hears.
   *
   * On a laptop the co-pilot's voice comes out of the same speakers the
   * microphone is sitting next to, so without this the pipeline transcribes its
   * own suggestion, treats it as the customer, and answers itself. Nothing in
   * the transcript would look obviously wrong — it just fills with plausible
   * sentences nobody said.
   *
   * Set to a generous estimate when speech starts and tightened when it ends,
   * so a dropped `onend` event unmutes on its own instead of deafening the call.
   */
  private quietUntil = 0;

  /**
   * Set while an interruption is being handled.
   *
   * `speechSynthesis.cancel()` fires the cancelled utterance's `onend`
   * *asynchronously*, and that handler also sets the quiet window — so the
   * short barge-in tail was written, then overwritten by the long one a moment
   * later. The microphone stayed deaf for the full 400 ms either way, which is
   * four hundred milliseconds of the customer's interruption thrown on the
   * floor. Measured, not reasoned about: both paths reported an identical
   * seven ticks of silence.
   */
  private bargingIn = false;

  constructor() {
    if (!this.supported) return;
    this.snap = { enabled: true, speaking: false };
    this.pickVoice();
    window.speechSynthesis.addEventListener?.("voiceschanged", () =>
      this.pickVoice(),
    );
    const prime = () => this.prime();
    window.addEventListener("pointerdown", prime, { once: true });
    window.addEventListener("keydown", prime, { once: true });
  }

  // -- subscription ----------------------------------------------------

  subscribe = (fn: () => void) => {
    this.listeners.add(fn);
    return () => void this.listeners.delete(fn);
  };

  getSnapshot = () => this.snap;

  private emit(next: Partial<Snapshot>) {
    const merged = { ...this.snap, ...next };
    if (merged.enabled === this.snap.enabled && merged.speaking === this.snap.speaking)
      return;
    this.snap = merged;
    this.listeners.forEach((fn) => fn());
  }

  // -- voice selection -------------------------------------------------

  private pickVoice() {
    const all = window.speechSynthesis.getVoices();
    if (!all.length) return; // fires again on voiceschanged
    for (const pattern of PREFERRED) {
      const hit = all.find((v) => pattern.test(v.lang));
      if (hit) {
        this.voice = hit;
        return;
      }
    }
    this.voice = all[0] ?? null;
  }

  /** Unlock the engine on the first user gesture, inaudibly. */
  private prime() {
    if (this.primed || !this.supported) return;
    this.primed = true;
    try {
      const u = new SpeechSynthesisUtterance(" ");
      u.volume = 0;
      window.speechSynthesis.speak(u);
    } catch {
      /* priming is best-effort; a failure just means the first line may be lost */
    }
  }

  // -- the microphone gate ---------------------------------------------

  /** True while the microphone must discard what it is hearing. */
  micMuted = () => this.quietUntil > Date.now();

  // -- speaking --------------------------------------------------------

  get enabled() {
    return this.snap.enabled;
  }

  setEnabled(on: boolean) {
    this.emit({ enabled: on });
    if (!on) this.cancel();
    else this.prime();
  }

  speak(text: string) {
    if (!this.supported || !this.snap.enabled) return;
    const clean = (text ?? "").trim();
    if (!clean) return;

    this.bargingIn = false;
    window.speechSynthesis.cancel();
    this.stopHeartbeat();

    const words = clean.split(/\s+/).length;
    const estimateMs = (words / WORDS_PER_SECOND) * 1000;
    // Mute generously up front. `onend` tightens it; if `onend` never arrives
    // the mic recovers by itself rather than staying deaf for the whole call.
    this.quietUntil = Date.now() + estimateMs * 1.6 + ECHO_TAIL_MS;

    const u = new SpeechSynthesisUtterance(clean);
    if (this.voice) u.voice = this.voice;
    u.lang = this.voice?.lang ?? "en-IN";
    u.rate = 1.02;
    u.pitch = 1;
    u.volume = 1;

    u.onstart = () => {
      this.emit({ speaking: true });
      this.startHeartbeat();
    };
    const done = () => {
      this.quietUntil =
        Date.now() + (this.bargingIn ? BARGE_IN_TAIL_MS : ECHO_TAIL_MS);
      this.bargingIn = false;
      this.stopHeartbeat();
      this.emit({ speaking: false });
    };
    u.onend = done;
    u.onerror = done;

    window.speechSynthesis.speak(u);
  }

  /**
   * Stop reading.
   *
   * `bargeIn` shortens the deaf window that follows. The normal tail exists to
   * let the speaker finish draining before the microphone reopens; when the
   * customer has interrupted, every one of those milliseconds is a word of
   * theirs thrown away. Speech has already been cancelled by then, so there is
   * far less left to echo — a short tail still covers the audio buffer.
   */
  cancel(opts: { bargeIn?: boolean } = {}) {
    if (!this.supported) return;
    // Set before cancelling: the utterance's own `onend` runs after this and
    // needs to know which tail applies.
    this.bargingIn = !!opts.bargeIn;
    window.speechSynthesis.cancel();
    this.quietUntil =
      Date.now() + (opts.bargeIn ? BARGE_IN_TAIL_MS : ECHO_TAIL_MS);
    this.stopHeartbeat();
    this.emit({ speaking: false });
  }

  /** Chrome truncates anything past ~15s unless the queue is nudged. */
  private startHeartbeat() {
    this.stopHeartbeat();
    this.heartbeat = setInterval(() => {
      if (!window.speechSynthesis.speaking) return this.stopHeartbeat();
      window.speechSynthesis.pause();
      window.speechSynthesis.resume();
    }, 8000);
  }

  private stopHeartbeat() {
    if (this.heartbeat) clearInterval(this.heartbeat);
    this.heartbeat = null;
  }
}

export const voice = new VoiceEngine();

export function useVoice() {
  const snap = useSyncExternalStore(voice.subscribe, voice.getSnapshot, voice.getSnapshot);
  const setEnabled = useCallback((on: boolean) => voice.setEnabled(on), []);
  const speak = useCallback((t: string) => voice.speak(t), []);
  const cancel = useCallback(() => voice.cancel(), []);
  return {
    supported: voice.supported,
    enabled: snap.enabled,
    speaking: snap.speaking,
    setEnabled,
    speak,
    cancel,
  };
}

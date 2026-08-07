import { useVoice } from "../lib/speech";

/**
 * The co-pilot's voice, as a control you can find.
 *
 * This lived as an unchecked box at the bottom of the microphone panel, below
 * the fold, defaulted off — so the feature existed and nobody ever heard it.
 * It belongs in the on-air strip beside the call state, because whether the
 * co-pilot is talking into your ear is a property of the call, not a setting.
 *
 * The test button is not a nicety. Speech synthesis fails silently in a dozen
 * ways — no voices installed, muted output device, a browser that never got its
 * user gesture — and all of them look identical to "the co-pilot has nothing to
 * say". One click, before the call, tells you which it is.
 */
export default function VoiceControl() {
  const { supported, enabled, speaking, setEnabled, speak, cancel } = useVoice();

  if (!supported) {
    return (
      <span className="tag" title="This browser has no speech synthesis">
        voice unavailable
      </span>
    );
  }

  return (
    <div className="flex items-center gap-1.5">
      <button
        type="button"
        onClick={() => setEnabled(!enabled)}
        aria-pressed={enabled}
        className={`tag ${enabled ? "tag-verified" : ""}`}
        style={enabled ? undefined : { color: "var(--graphite)" }}
        title={
          enabled
            ? "The co-pilot reads each suggestion into your ear"
            : "The co-pilot is silent — you read the line yourself"
        }
      >
        <span
          aria-hidden="true"
          className={speaking ? "live-dot" : ""}
          style={{
            display: "inline-block",
            width: 6,
            height: 6,
            borderRadius: 999,
            marginRight: 6,
            verticalAlign: "middle",
            background: enabled ? "var(--verified)" : "var(--hairline)",
          }}
        />
        {speaking ? "reading to you" : enabled ? "voice on" : "voice off"}
      </button>

      <button
        type="button"
        onClick={() =>
          speaking
            ? cancel()
            : speak(
                "Voice check. If you can hear this, the co-pilot will read each suggestion into your ear.",
              )
        }
        className="text-[11px] underline"
        style={{ color: "var(--graphite)" }}
      >
        {speaking ? "stop" : "test"}
      </button>
    </div>
  );
}

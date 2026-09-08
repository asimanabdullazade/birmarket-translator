/**
 * Phase 9 restyle: a grouped cluster of four rows, matching the provided
 * UI mockup (Translation ON/OFF, Captions ON/OFF, Original audio %,
 * Translation audio %).
 *
 * - Translation ON/OFF: renames + reuses the pre-existing mute button/
 *   `setMuted` (Step 6) -- inverted semantics (ON means NOT muted). Also
 *   now hides translated caption text, not just audio -- see App.jsx.
 * - Captions ON/OFF: new, frontend-only -- hides the whole transcript
 *   panel (App.jsx), independent of translation state.
 * - Original audio: new (Phase 9) -- a local mic self-monitor ("sidetone"),
 *   see useTranslationSession's `originalVolume`/`setOriginalVolume` in
 *   hooks/useWebSocket.js and the feedback-risk caveat in
 *   audio/audioCapture.js. Off (0) by default, not muted by Translation
 *   OFF -- it's the user's own voice, unrelated to translation state.
 * - Translation audio: renamed from "Translation volume" (Step 6),
 *   unchanged functionality -- disabled while Translation is OFF, same as
 *   before.
 */
export default function AudioControls({
  translationMuted,
  onToggleTranslationMuted,
  captionsOn,
  onToggleCaptions,
  translationVolume,
  onTranslationVolumeChange,
  originalVolume,
  onOriginalVolumeChange,
}) {
  const translationOn = !translationMuted;

  return (
    <div className="audio-controls">
      <div className="toggle-row">
        <span className="field-label">Translation</span>
        <button
          type="button"
          className={`btn-toggle${translationOn ? " btn-toggle-on" : ""}`}
          onClick={() => onToggleTranslationMuted(translationOn)}
          aria-pressed={translationOn}
        >
          {translationOn ? "ON" : "OFF"}
        </button>
      </div>

      <div className="toggle-row">
        <span className="field-label">Captions</span>
        <button
          type="button"
          className={`btn-toggle${captionsOn ? " btn-toggle-on" : ""}`}
          onClick={() => onToggleCaptions(!captionsOn)}
          aria-pressed={captionsOn}
        >
          {captionsOn ? "ON" : "OFF"}
        </button>
      </div>

      <div className="slider-row">
        <label className="field-label" htmlFor="original-audio-volume">
          Original audio
        </label>
        <input
          id="original-audio-volume"
          type="range"
          min="0"
          max="1"
          step="0.01"
          value={originalVolume}
          onChange={(event) => onOriginalVolumeChange(Number(event.target.value))}
        />
        <p className="field-hint">
          Off by default. Turning this up without headphones can cause audio feedback.
        </p>
      </div>

      <div className="slider-row">
        <label className="field-label" htmlFor="translation-audio-volume">
          Translation audio
        </label>
        <input
          id="translation-audio-volume"
          type="range"
          min="0"
          max="1"
          step="0.01"
          value={translationVolume}
          onChange={(event) => onTranslationVolumeChange(Number(event.target.value))}
          disabled={translationMuted}
        />
      </div>
    </div>
  );
}

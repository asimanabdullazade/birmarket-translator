/**
 * Volume slider + mute button for the incoming synthesized translation
 * audio (Step 6) -- see useTranslationSession's `volume`/`setVolume`/
 * `muted`/`setMuted` in hooks/useWebSocket.js for what these actually
 * control (a GainNode in audio/audioPlayback.js, plus a `set_muted`
 * message to the server so it can skip synthesis while muted).
 */
export default function AudioControls({ volume, onVolumeChange, muted, onToggleMute }) {
  return (
    <div className="audio-controls">
      <label className="field-label" htmlFor="translation-volume">
        Translation volume
      </label>
      <input
        id="translation-volume"
        type="range"
        min="0"
        max="1"
        step="0.01"
        value={volume}
        onChange={(event) => onVolumeChange(Number(event.target.value))}
        disabled={muted}
      />
      <button
        type="button"
        className={`btn btn-mute${muted ? " btn-mute-active" : ""}`}
        onClick={() => onToggleMute(!muted)}
      >
        {muted ? "Unmute translation" : "Mute translation"}
      </button>
    </div>
  );
}

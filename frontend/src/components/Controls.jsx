/**
 * Start / Pause-Resume / Stop. Pause/resume (Phase 9) stops/resumes
 * capture without ending the session -- see useTranslationSession's
 * `pause`/`resume` in hooks/useWebSocket.js. Enabled whenever a session is
 * active at all (`isRunning`, which already covers "paused" and
 * "reconnecting" -- see App.jsx), same as Stop.
 */
export default function Controls({ isRunning, isPaused, onStart, onStop, onPause, onResume, startDisabled }) {
  return (
    <div className="controls">
      <button className="btn btn-start" onClick={onStart} disabled={isRunning || startDisabled}>
        Start Translation
      </button>
      <button className="btn btn-pause" onClick={isPaused ? onResume : onPause} disabled={!isRunning}>
        {isPaused ? "Resume" : "Pause"}
      </button>
      <button className="btn btn-stop" onClick={onStop} disabled={!isRunning}>
        Stop
      </button>
    </div>
  );
}

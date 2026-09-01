export default function Controls({ isRunning, onStart, onStop, startDisabled }) {
  return (
    <div className="controls">
      <button className="btn btn-start" onClick={onStart} disabled={isRunning || startDisabled}>
        Start Translation
      </button>
      <button className="btn btn-stop" onClick={onStop} disabled={!isRunning}>
        Stop
      </button>
    </div>
  );
}

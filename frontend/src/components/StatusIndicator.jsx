// Phase 9: "paused" is a real server status (see StatusValue in
// backend/models/schemas.py). "reconnecting" is frontend-only -- the
// server never sends it, see "Reconnection" in hooks/useWebSocket.js.
const STATUS_LABELS = {
  idle: "Idle",
  connected: "Connected",
  listening: "Listening",
  translating: "Translating",
  paused: "Paused",
  reconnecting: "Reconnecting...",
  error: "Error",
};

const STATUS_CLASSES = {
  idle: "status-idle",
  connected: "status-connected",
  listening: "status-listening",
  translating: "status-translating",
  paused: "status-paused",
  reconnecting: "status-reconnecting",
  error: "status-error",
};

export default function StatusIndicator({ status, errorMessage }) {
  const label = STATUS_LABELS[status] || status;
  const className = STATUS_CLASSES[status] || "status-idle";

  return (
    <div className="status-row">
      <span className={`status-dot ${className}`} />
      <span className="status-text">Status: {label}</span>
      {status === "error" && errorMessage && <span className="status-error-detail">({errorMessage})</span>}
    </div>
  );
}

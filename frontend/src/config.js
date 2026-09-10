export const BACKEND_HTTP_URL =
  import.meta.env.VITE_BACKEND_HTTP_URL || "http://localhost:8000";

export const BACKEND_WS_URL =
  import.meta.env.VITE_BACKEND_WS_URL || "ws://localhost:8000/ws/translate";

// Must match config/settings.py AUDIO_SAMPLE_RATE on the backend.
export const AUDIO_SAMPLE_RATE = 16000;

// Target size of each audio chunk sent to the backend over the WebSocket,
// in milliseconds. Kept small (100-300ms) for low latency -- batching
// happens inside public/pcmWorkletProcessor.js.
export const AUDIO_SEND_CHUNK_MS = 200;

// Fallback language list, used if the backend /languages call fails
// (e.g. backend not started yet). Kept in sync with config/languages.py --
// these are the MVP launch languages.
export const FALLBACK_LANGUAGES = [
  { code: "en", name: "English" },
  { code: "az", name: "Azerbaijani" },
  { code: "ru", name: "Russian" },
];

// Phase 11 (meeting broadcast mode): builds the listener WebSocket URL for
// one (meetingId, lang) room -- see backend/websocket/meeting_handlers.py's
// /ws/meeting/{meeting_id}/listen route. Derived from BACKEND_HTTP_URL
// (http->ws / https->wss) rather than BACKEND_WS_URL, since that constant
// is hardcoded to the single-user app's own /ws/translate path and isn't a
// usable base for a different route.
export function meetingListenUrl(meetingId, lang) {
  const wsBase = BACKEND_HTTP_URL.replace(/^http/, "ws");
  return `${wsBase}/ws/meeting/${encodeURIComponent(meetingId)}/listen?lang=${encodeURIComponent(lang)}`;
}

// Phase 11 (meeting broadcast mode): builds the ingest WebSocket URL for a
// meeting -- see backend/websocket/meeting_handlers.py's
// /ws/meeting/{meeting_id}/ingest route. Used by the dev-only mic
// broadcaster page (src/MeetingBroadcast.jsx) and matches the same wire
// contract _dev_stream_meeting_audio.py uses. No ?lang= -- the ingest side
// never picks a target language, see meeting_handlers.py.
export function meetingIngestUrl(meetingId) {
  const wsBase = BACKEND_HTTP_URL.replace(/^http/, "ws");
  return `${wsBase}/ws/meeting/${encodeURIComponent(meetingId)}/ingest`;
}

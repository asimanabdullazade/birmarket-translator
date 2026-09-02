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

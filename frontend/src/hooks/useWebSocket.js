import { useCallback, useRef, useState } from "react";
import { BACKEND_WS_URL, AUDIO_SAMPLE_RATE } from "../config.js";
import { MicCapture } from "../audio/audioCapture.js";
import { TranslationAudioPlayer } from "../audio/audioPlayback.js";

/**
 * Owns the WebSocket connection, the mic capture pipeline, and the
 * status/transcript/translation state driven by server messages -- see
 * backend/models/schemas.py for the exact message shapes this expects.
 *
 * Transcript/translation state (Step 4)
 * --------------------------------------
 * Every transcript/translation message carries `is_final` and a
 * `timestamp` shared by every message about the same phrase (partial(s)
 * then one final). We keep two pieces of state instead of one:
 *
 * - `history`: one entry per *finished* phrase (`is_final: true`), keyed by
 *   timestamp, holding the final source text and -- once it arrives -- the
 *   translation. This is the only data meant to be kept ("Only the final
 *   version should eventually be stored").
 * - `livePartial`: the current in-progress phrase's interim transcript, if
 *   any (`{ timestamp, text }`). It's replaced in place as new partials
 *   arrive for the same phrase (never appended/accumulated), and cleared
 *   once that phrase's final transcript shows up -- at which point the text
 *   lives in `history` instead.
 *
 * Translation audio playback (Step 6)
 * ------------------------------------
 * `audio` messages (one or more per finished phrase, streamed as each
 * chunk is synthesized -- see backend/websocket/handlers.py) are handed
 * straight to a `TranslationAudioPlayer` (audio/audioPlayback.js), which
 * queues and plays them back to back with no overlap. `volume` and
 * `muted` control that player's GainNode; `muted` is also sent to the
 * server as a `set_muted` message so it can skip the TTS call entirely
 * rather than synthesizing audio nobody will hear.
 *
 * Latency measurement (Step 7)
 * -----------------------------
 * The server already knows when each phrase's speech was detected, when
 * its audio finished capturing, and when transcription/translation/TTS
 * each completed -- everything except the one moment that only exists in
 * the browser: when the translated audio actually starts being audible.
 * The *first* `audio` message for each phrase (tracked via
 * `reportedPhrasesRef`, keyed by the phrase's `timestamp`) gets that
 * moment from `TranslationAudioPlayer.enqueue()`'s resolved value and
 * reports it back as an `audio_played` message, so
 * backend/websocket/handlers.py can log a complete start-to-finish
 * latency breakdown for that phrase. See "Measuring latency" in the
 * README.
 */
export function useTranslationSession() {
  const [status, setStatus] = useState("idle"); // idle | connected | listening | translating | error
  const [history, setHistory] = useState([]);
  const [livePartial, setLivePartial] = useState(null); // { timestamp, text } | null
  const [errorMessage, setErrorMessage] = useState(null);
  const [volume, setVolumeState] = useState(1);
  const [muted, setMutedState] = useState(false);

  const socketRef = useRef(null);
  const micRef = useRef(null);
  const playerRef = useRef(null);
  if (!playerRef.current) {
    playerRef.current = new TranslationAudioPlayer();
  }
  // Mirrors `muted` for code that can't wait for a re-render (the `start`
  // callback's `onopen` closure, captured once per call) -- see `start` below.
  const mutedRef = useRef(false);
  // Step 7: phrase timestamps we've already sent an `audio_played` report
  // for, so only the *first* audio chunk of a phrase gets reported (later
  // chunks of a multi-chunk phrase don't need their own latency number).
  const reportedPhrasesRef = useRef(new Set());

  // Create-or-update the history entry for a given phrase timestamp.
  const upsertEntry = useCallback((timestamp, patch) => {
    setHistory((current) => {
      const index = current.findIndex((entry) => entry.timestamp === timestamp);
      if (index === -1) {
        return [
          ...current,
          { timestamp, sourceText: "", translationText: "", detectedLanguage: null, ...patch },
        ];
      }
      const next = [...current];
      next[index] = { ...next[index], ...patch };
      return next;
    });
  }, []);

  const setVolume = useCallback((nextVolume) => {
    setVolumeState(nextVolume);
    playerRef.current.setVolume(nextVolume);
  }, []);

  const setMuted = useCallback((nextMuted) => {
    setMutedState(nextMuted);
    mutedRef.current = nextMuted;
    playerRef.current.setMuted(nextMuted);
    // Also tell the server, mid-session, so it can skip the TTS call
    // entirely rather than synthesizing audio we're about to discard --
    // see SetMutedMessage in backend/models/schemas.py.
    if (socketRef.current?.readyState === WebSocket.OPEN) {
      socketRef.current.send(JSON.stringify({ type: "set_muted", muted: nextMuted }));
    }
  }, []);

  const stop = useCallback(() => {
    if (socketRef.current?.readyState === WebSocket.OPEN) {
      socketRef.current.send(JSON.stringify({ type: "stop" }));
      socketRef.current.close();
    }
    socketRef.current = null;

    micRef.current?.stop();
    micRef.current = null;

    playerRef.current.stop();

    setStatus("idle");
  }, []);

  const start = useCallback(
    ({ sourceLang, targetLang, microphoneId }) => {
      setErrorMessage(null);
      setHistory([]);
      setLivePartial(null);
      reportedPhrasesRef.current = new Set();

      const socket = new WebSocket(BACKEND_WS_URL);
      socketRef.current = socket;

      socket.onopen = () => {
        socket.send(
          JSON.stringify({
            type: "start",
            source_lang: sourceLang,
            target_lang: targetLang,
            sample_rate: AUDIO_SAMPLE_RATE,
          })
        );
        // Sync whatever mute preference carried over from a previous
        // session (the server always starts a new session unmuted) --
        // read via the ref, not the `muted` state var, since this closure
        // was created once when `start` was called and won't see later
        // re-renders.
        if (mutedRef.current) {
          socket.send(JSON.stringify({ type: "set_muted", muted: true }));
        }
      };

      socket.onmessage = (event) => {
        const message = JSON.parse(event.data);
        switch (message.type) {
          case "status":
            setStatus(message.status);
            if (message.status === "error" && message.detail) {
              setErrorMessage(message.detail);
            }
            break;
          case "transcript":
            if (message.is_final) {
              // Phrase is done -- move it into history (the only version
              // meant to be kept) and drop the live partial for it.
              upsertEntry(message.timestamp, {
                sourceText: message.text,
                detectedLanguage: message.detected_language ?? null,
              });
              setLivePartial((current) => (current?.timestamp === message.timestamp ? null : current));
            } else {
              // Still-in-progress phrase -- replace in place, never append.
              setLivePartial({ timestamp: message.timestamp, text: message.text });
            }
            break;
          case "translation":
            // Attach to the matching (existing or not-yet-created) history
            // entry. `message.text` is always the FULL translation
            // accumulated so far, whether this is the final version
            // (is_final: true) or a growing incremental one sent while the
            // phrase is still being spoken (Phase 8's streaming
            // translation, is_final: false) -- either way a plain
            // overwrite is correct and needs no extra handling here, the
            // same way a growing partial transcript already works above.
            upsertEntry(message.timestamp, { translationText: message.text });
            break;
          case "audio": {
            // One synthesized-speech chunk for a translated phrase (Step
            // 6) -- queue it for gapless playback. Not correlated with
            // `history` by timestamp for display purposes; the player
            // handles ordering/overlap on its own.
            const isFirstChunkForPhrase = !reportedPhrasesRef.current.has(message.timestamp);
            const playedPromise = playerRef.current.enqueue(message.audio_base64);
            if (isFirstChunkForPhrase) {
              // Step 7: only the first chunk's actual playback moment is
              // worth reporting -- see the module docstring above.
              reportedPhrasesRef.current.add(message.timestamp);
              playedPromise.then((playedAtMs) => {
                if (playedAtMs != null && socketRef.current?.readyState === WebSocket.OPEN) {
                  socketRef.current.send(
                    JSON.stringify({ type: "audio_played", timestamp: message.timestamp, played_at_ms: playedAtMs })
                  );
                }
              });
            }
            break;
          }
          case "error":
            setErrorMessage(message.message);
            break;
          default:
            break;
        }
      };

      socket.onerror = () => {
        setStatus("error");
        setErrorMessage("WebSocket connection error");
      };

      socket.onclose = () => {
        micRef.current?.stop();
        micRef.current = null;
        setStatus((current) => (current === "error" ? current : "idle"));
      };

      // Start the mic once the socket has sent "start"; audio frames sent
      // before the server processes "start" are harmless to buffer briefly,
      // but we wait for the socket to be open to avoid dropping them.
      const beginCapture = async () => {
        const mic = new MicCapture({
          deviceId: microphoneId,
          onPCMChunk: (arrayBuffer) => {
            if (socket.readyState === WebSocket.OPEN) {
              socket.send(arrayBuffer);
            }
          },
        });
        micRef.current = mic;
        try {
          await mic.start();
        } catch (err) {
          setErrorMessage(err.message || "Could not access microphone");
          setStatus("error");
        }
      };

      if (socket.readyState === WebSocket.OPEN) {
        beginCapture();
      } else {
        socket.addEventListener("open", beginCapture, { once: true });
      }
    },
    [upsertEntry]
  );

  return { status, history, livePartial, errorMessage, volume, setVolume, muted, setMuted, start, stop };
}

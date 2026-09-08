import { useCallback, useRef, useState } from "react";
import { BACKEND_WS_URL, AUDIO_SAMPLE_RATE } from "../config.js";
import { MicCapture } from "../audio/audioCapture.js";
import { TranslationAudioPlayer } from "../audio/audioPlayback.js";

// Phase 9 (error recovery): reconnect backoff shape -- exponential, capped,
// with a hard attempt limit before giving up and surfacing a terminal
// error. See "Reconnection" below.
const MAX_RECONNECT_ATTEMPTS = 5;
const RECONNECT_BASE_DELAY_MS = 500;
const RECONNECT_MAX_DELAY_MS = 8000;

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
 *
 * Pause/resume (Phase 9)
 * -----------------------
 * `pause()`/`resume()` send the matching control message (see
 * PauseMessage/ResumeMessage in backend/models/schemas.py), gate the mic
 * (MicCapture.pause()/resume() in audioCapture.js), and optimistically set
 * status locally -- the same "set it immediately, let the server's own
 * status message confirm it shortly after" pattern `stop()` already uses
 * for "idle". `pausedRef` mirrors the paused state for code that can't
 * wait for a re-render (the reconnect flow below re-asserts pause on the
 * server after a reconnect, since a reconnect always opens a brand new
 * backend session with no memory of the old one being paused).
 *
 * Original-audio monitor (Phase 9)
 * -----------------------------------
 * `originalVolume`/`setOriginalVolume` mirror `volume`/`setVolume`'s
 * pattern, wired to `MicCapture.setMonitorVolume` (audioCapture.js)
 * instead of `TranslationAudioPlayer.setVolume` -- lets the user hear
 * their own mic locally. Defaults to 0 (off); see the feedback-risk
 * caveat in audioCapture.js's module docstring and the "Original audio"
 * slider's caption in AudioControls.jsx.
 *
 * Reconnection (Phase 9)
 * -------------------------
 * There is no session-resumption support on the backend at all (confirmed
 * by reading backend/main.py -- no session IDs anywhere), so a reconnect
 * always means: open a new WebSocket, send a fresh `start` with the same
 * params, and let the backend build a brand-new session. `attachSocketHandlers`
 * is the shared onopen/onmessage/onerror/onclose wiring used for both the
 * very first connection and every reconnect, so that logic isn't
 * duplicated. `intentionalCloseRef` is what tells `onclose` whether a
 * disconnect was expected (the user clicked Stop) or not (network blip,
 * server restart, etc.) -- only the latter triggers `attemptReconnect()`.
 * `onerror` is a deliberate no-op: `onclose` is the single decision point
 * for idle vs. reconnect vs. terminal error, so nothing here can race or
 * clobber a more-informed decision onclose is about to make.
 *
 * The same `MicCapture` instance (and its already-granted mic permission)
 * survives across any number of reconnects -- only the WebSocket itself is
 * re-established; `beginCapture` runs exactly once, on the original
 * `start()` call. `history`/`livePartial` are likewise preserved across a
 * reconnect (only `start()` resets them). Known, accepted limitation:
 * mic audio arriving while the socket is down is dropped, not buffered
 * (there's no good place to hold it without reinventing VAD client-side),
 * so a phrase that was mid-utterance exactly when an *unexpected*
 * disconnect happens is lost -- unlike a user-initiated `pause`, which
 * always cleanly flushes server-side first.
 */
export function useTranslationSession() {
  const [status, setStatus] = useState("idle"); // idle | connected | listening | translating | paused | reconnecting | error
  const [history, setHistory] = useState([]);
  const [livePartial, setLivePartial] = useState(null); // { timestamp, text } | null
  const [errorMessage, setErrorMessage] = useState(null);
  const [volume, setVolumeState] = useState(1);
  const [muted, setMutedState] = useState(false);
  const [originalVolume, setOriginalVolumeState] = useState(0);

  const socketRef = useRef(null);
  const micRef = useRef(null);
  const playerRef = useRef(null);
  if (!playerRef.current) {
    playerRef.current = new TranslationAudioPlayer();
  }
  // Mirrors `muted` for code that can't wait for a re-render (the `start`
  // callback's `onopen` closure, captured once per call) -- see `start` below.
  const mutedRef = useRef(false);
  // Mirrors `originalVolume` for the same reason -- re-applied to a freshly
  // created MicCapture instance in `beginCapture`.
  const originalVolumeRef = useRef(0);
  // Phase 9: mirrors the paused state, re-asserted on the server after a
  // reconnect (see "Reconnection" above).
  const pausedRef = useRef(false);
  // Phase 9: true only when the user (or a hard stop) intentionally closed
  // the socket -- distinguishes an expected close (go idle) from an
  // unexpected one (reconnect). Set right before `stop()`'s own `close()`.
  const intentionalCloseRef = useRef(false);
  // Phase 9: remembers the params from the original `start()` call so a
  // reconnect can resend an equivalent `start` message.
  const sessionParamsRef = useRef(null);
  const reconnectAttemptsRef = useRef(0);
  const reconnectTimeoutRef = useRef(null);
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

  const setOriginalVolume = useCallback((nextVolume) => {
    setOriginalVolumeState(nextVolume);
    originalVolumeRef.current = nextVolume;
    micRef.current?.setMonitorVolume(nextVolume);
  }, []);

  const stop = useCallback(() => {
    intentionalCloseRef.current = true;
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
      reconnectTimeoutRef.current = null;
    }
    if (socketRef.current?.readyState === WebSocket.OPEN) {
      socketRef.current.send(JSON.stringify({ type: "stop" }));
      socketRef.current.close();
    }
    socketRef.current = null;

    micRef.current?.stop();
    micRef.current = null;

    playerRef.current.stop();
    pausedRef.current = false;

    setStatus("idle");
  }, []);

  // Phase 9: pause/resume without tearing the session down -- see the
  // module docstring.
  const pause = useCallback(() => {
    pausedRef.current = true;
    if (socketRef.current?.readyState === WebSocket.OPEN) {
      socketRef.current.send(JSON.stringify({ type: "pause" }));
    }
    micRef.current?.pause();
    // Optimistic, same pattern stop() already uses for "idle" -- the
    // server's own `status: paused` message confirms it shortly after.
    setStatus("paused");
  }, []);

  const resume = useCallback(() => {
    pausedRef.current = false;
    if (socketRef.current?.readyState === WebSocket.OPEN) {
      socketRef.current.send(JSON.stringify({ type: "resume" }));
    }
    micRef.current?.resume();
    setStatus("listening");
  }, []);

  const start = useCallback(
    ({ sourceLang, targetLang, microphoneId }) => {
      setErrorMessage(null);
      setHistory([]);
      setLivePartial(null);
      reportedPhrasesRef.current = new Set();
      intentionalCloseRef.current = false;
      reconnectAttemptsRef.current = 0;
      pausedRef.current = false;
      sessionParamsRef.current = { sourceLang, targetLang, microphoneId };

      // Shared onopen/onmessage/onerror/onclose wiring for both the
      // original connection and every reconnect -- see "Reconnection" in
      // the module docstring.
      function attachSocketHandlers(socket, { isReconnect = false } = {}) {
        socket.onopen = () => {
          const params = sessionParamsRef.current;
          socket.send(
            JSON.stringify({
              type: "start",
              source_lang: params.sourceLang,
              target_lang: params.targetLang,
              sample_rate: AUDIO_SAMPLE_RATE,
            })
          );
          // Sync whatever mute preference carried over from a previous
          // session (the server always starts a new session unmuted) --
          // read via the ref, not the `muted` state var, since this
          // closure was created once when attachSocketHandlers ran and
          // won't see later re-renders.
          if (mutedRef.current) {
            socket.send(JSON.stringify({ type: "set_muted", muted: true }));
          }
          if (isReconnect) {
            reconnectAttemptsRef.current = 0;
            if (reconnectTimeoutRef.current) {
              clearTimeout(reconnectTimeoutRef.current);
              reconnectTimeoutRef.current = null;
            }
            // A reconnect always opens a brand-new backend session (no
            // session-resumption support -- see the module docstring), so
            // if we were paused before the drop, re-assert it here rather
            // than silently resuming capture on the new session.
            if (pausedRef.current) {
              socket.send(JSON.stringify({ type: "pause" }));
            }
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

        // Deliberate no-op -- see "Reconnection" in the module docstring
        // for why onclose (not onerror) is the single decision point here.
        socket.onerror = () => {};

        socket.onclose = () => {
          if (socketRef.current !== socket) {
            // A stale socket's close firing after we've already moved on
            // (e.g. the previous socket, right after a reconnect swapped
            // in a new one) -- ignore it.
            return;
          }
          if (intentionalCloseRef.current) {
            micRef.current?.stop();
            micRef.current = null;
            setStatus((current) => (current === "error" ? current : "idle"));
            return;
          }
          attemptReconnect();
        };
      }

      function attemptReconnect() {
        if (reconnectAttemptsRef.current >= MAX_RECONNECT_ATTEMPTS) {
          setStatus("error");
          setErrorMessage(
            "Lost connection to the server and couldn't reconnect after several attempts. Click Start to try again."
          );
          micRef.current?.stop();
          micRef.current = null;
          socketRef.current = null;
          return;
        }
        reconnectAttemptsRef.current += 1;
        setStatus("reconnecting");
        const backoffMs = Math.min(
          RECONNECT_BASE_DELAY_MS * 2 ** (reconnectAttemptsRef.current - 1),
          RECONNECT_MAX_DELAY_MS
        );
        reconnectTimeoutRef.current = setTimeout(() => {
          reconnectTimeoutRef.current = null;
          const socket = new WebSocket(BACKEND_WS_URL);
          socketRef.current = socket;
          attachSocketHandlers(socket, { isReconnect: true });
        }, backoffMs);
      }

      const socket = new WebSocket(BACKEND_WS_URL);
      socketRef.current = socket;
      attachSocketHandlers(socket, { isReconnect: false });

      // Start the mic once the socket has sent "start"; audio frames sent
      // before the server processes "start" are harmless to buffer briefly,
      // but we wait for the socket to be open to avoid dropping them. Runs
      // exactly once per start() call -- a reconnect re-establishes only
      // the WebSocket, never this mic/worklet pipeline (see "Reconnection"
      // in the module docstring).
      const beginCapture = async () => {
        const mic = new MicCapture({
          deviceId: microphoneId,
          onPCMChunk: (arrayBuffer) => {
            // Read the CURRENT socket via the ref, not any specific
            // `socket` value closed over here -- a reconnect swaps in a
            // new WebSocket without recreating this callback, so closing
            // over one fixed `socket` would leave this forever checking
            // an old, closed connection after a reconnect.
            if (socketRef.current?.readyState === WebSocket.OPEN) {
              socketRef.current.send(arrayBuffer);
            }
          },
        });
        micRef.current = mic;
        try {
          await mic.start();
          mic.setMonitorVolume(originalVolumeRef.current);
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

  return {
    status,
    history,
    livePartial,
    errorMessage,
    volume,
    setVolume,
    muted,
    setMuted,
    originalVolume,
    setOriginalVolume,
    start,
    stop,
    pause,
    resume,
  };
}

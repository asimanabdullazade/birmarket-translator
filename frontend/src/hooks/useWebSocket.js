import { useCallback, useRef, useState } from "react";
import { BACKEND_WS_URL, AUDIO_SAMPLE_RATE } from "../config.js";
import { MicCapture } from "../audio/audioCapture.js";

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
 */
export function useTranslationSession() {
  const [status, setStatus] = useState("idle"); // idle | connected | listening | translating | error
  const [history, setHistory] = useState([]);
  const [livePartial, setLivePartial] = useState(null); // { timestamp, text } | null
  const [errorMessage, setErrorMessage] = useState(null);

  const socketRef = useRef(null);
  const micRef = useRef(null);

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

  const stop = useCallback(() => {
    if (socketRef.current?.readyState === WebSocket.OPEN) {
      socketRef.current.send(JSON.stringify({ type: "stop" }));
      socketRef.current.close();
    }
    socketRef.current = null;

    micRef.current?.stop();
    micRef.current = null;

    setStatus("idle");
  }, []);

  const start = useCallback(
    ({ sourceLang, targetLang, microphoneId }) => {
      setErrorMessage(null);
      setHistory([]);
      setLivePartial(null);

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
            // Translations are always final -- attach to the matching
            // (existing or not-yet-created) history entry.
            upsertEntry(message.timestamp, { translationText: message.text });
            break;
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

  return { status, history, livePartial, errorMessage, start, stop };
}

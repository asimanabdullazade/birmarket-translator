import { useCallback, useRef, useState } from "react";
import { BACKEND_WS_URL, AUDIO_SAMPLE_RATE } from "../config.js";
import { MicCapture } from "../audio/audioCapture.js";

/**
 * Owns the WebSocket connection, the mic capture pipeline, and the
 * status/transcript/translation state driven by server messages -- see
 * backend/models/schemas.py for the exact message shapes this expects.
 */
export function useTranslationSession() {
  const [status, setStatus] = useState("idle"); // idle | connected | listening | translating | error
  const [transcript, setTranscript] = useState("");
  const [translation, setTranslation] = useState("");
  const [errorMessage, setErrorMessage] = useState(null);

  const socketRef = useRef(null);
  const micRef = useRef(null);

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

  const start = useCallback(({ sourceLang, targetLang, microphoneId }) => {
    setErrorMessage(null);
    setTranscript("");
    setTranslation("");

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
          setTranscript(message.text);
          break;
        case "translation":
          setTranslation(message.text);
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
  }, []);

  return { status, transcript, translation, errorMessage, start, stop };
}

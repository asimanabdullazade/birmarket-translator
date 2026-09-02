import { useEffect, useState } from "react";
import LanguageSelector from "./components/LanguageSelector.jsx";
import DeviceSelector from "./components/DeviceSelector.jsx";
import StatusIndicator from "./components/StatusIndicator.jsx";
import Controls from "./components/Controls.jsx";
import { useAudioDevices } from "./hooks/useAudioDevices.js";
import { useTranslationSession } from "./hooks/useWebSocket.js";
import { BACKEND_HTTP_URL, FALLBACK_LANGUAGES } from "./config.js";

export default function App() {
  const [languages, setLanguages] = useState(FALLBACK_LANGUAGES);
  const [sourceLang, setSourceLang] = useState("en");
  const [targetLang, setTargetLang] = useState("az");
  const [microphoneId, setMicrophoneId] = useState("");
  const [outputId, setOutputId] = useState("");

  const { microphones, outputs, permissionError } = useAudioDevices();
  const { status, history, livePartial, errorMessage, start, stop } = useTranslationSession();

  const isRunning = status !== "idle" && status !== "error";

  useEffect(() => {
    fetch(`${BACKEND_HTTP_URL}/languages`)
      .then((res) => res.json())
      .then((data) => Array.isArray(data) && data.length > 0 && setLanguages(data))
      .catch(() => {
        // Backend not reachable yet -- keep the fallback list so the UI still works.
      });
  }, []);

  useEffect(() => {
    if (!microphoneId && microphones.length > 0) {
      setMicrophoneId(microphones[0].deviceId);
    }
  }, [microphones, microphoneId]);

  useEffect(() => {
    if (!outputId && outputs.length > 0) {
      setOutputId(outputs[0].deviceId);
    }
  }, [outputs, outputId]);

  const handleStart = () => {
    start({ sourceLang, targetLang, microphoneId });
  };

  return (
    <div className="app">
      <h1>Real-Time Speech Translator</h1>

      <div className="panel">
        <div className="row">
          <LanguageSelector
            label="Source:"
            languages={languages}
            value={sourceLang}
            onChange={setSourceLang}
            disabled={isRunning}
          />
          <LanguageSelector
            label="Translate to:"
            languages={languages}
            value={targetLang}
            onChange={setTargetLang}
            disabled={isRunning}
          />
        </div>

        <div className="row">
          <DeviceSelector
            label="Microphone:"
            devices={microphones}
            value={microphoneId}
            onChange={setMicrophoneId}
            disabled={isRunning}
            emptyLabel="No microphones found"
          />
          <DeviceSelector
            label="Output:"
            devices={outputs}
            value={outputId}
            onChange={setOutputId}
            disabled={isRunning}
            emptyLabel="No output devices found"
          />
        </div>

        {permissionError && <p className="permission-warning">Microphone access needed: {permissionError}</p>}

        <Controls isRunning={isRunning} onStart={handleStart} onStop={stop} startDisabled={!!permissionError} />

        <StatusIndicator status={status} errorMessage={errorMessage} />

        {/* Step 4: chat-style transcript history (final phrases, source +
            translation) plus a live line for the phrase still being
            spoken. Only finished phrases (history) are meant to be kept --
            the live partial is replaced in place and never accumulated. */}
        <div className="transcript-panel">
          {history.length === 0 && !livePartial && (
            <p className="transcript-empty">Your transcript will appear here once you start speaking.</p>
          )}

          {history.map((entry) => (
            <div className="transcript-entry" key={entry.timestamp}>
              <div className="transcript-line">
                <span className="transcript-speaker">You:</span> {entry.sourceText}
                {entry.detectedLanguage && (
                  <span className="transcript-detected-lang"> ({entry.detectedLanguage})</span>
                )}
              </div>
              {entry.translationText && (
                <div className="translation-line">
                  <span className="transcript-speaker">{targetLang}:</span> {entry.translationText}
                </div>
              )}
            </div>
          ))}

          {livePartial && (
            <div className="transcript-entry transcript-entry-partial">
              <div className="transcript-line transcript-line-partial">
                <span className="transcript-speaker">You:</span> {livePartial.text}
                <span className="partial-cursor" aria-hidden="true" />
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

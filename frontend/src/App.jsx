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
  const { status, transcript, translation, errorMessage, start, stop } = useTranslationSession();

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

        {(transcript || translation) && (
          <div className="results">
            <div>
              <span className="results-label">Transcript ({sourceLang}):</span> {transcript}
            </div>
            <div>
              <span className="results-label">Translation ({targetLang}):</span> {translation}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

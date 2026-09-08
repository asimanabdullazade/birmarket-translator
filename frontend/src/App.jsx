import { useEffect, useState } from "react";
import LanguageSelector from "./components/LanguageSelector.jsx";
import DeviceSelector from "./components/DeviceSelector.jsx";
import StatusIndicator from "./components/StatusIndicator.jsx";
import Controls from "./components/Controls.jsx";
import AudioControls from "./components/AudioControls.jsx";
import { useAudioDevices } from "./hooks/useAudioDevices.js";
import { useTranslationSession } from "./hooks/useWebSocket.js";
import { BACKEND_HTTP_URL, FALLBACK_LANGUAGES } from "./config.js";

// Phase 9: source-language auto-detect. Deliberately client-side-only,
// prepended to the source dropdown's options rather than added to
// config.js's language list -- "auto" must never be selectable as a
// *target* (the target selector doesn't get this), and the backend
// validates which providers actually support it (see
// _AUTO_SOURCE_LANG_PROVIDERS in backend/websocket/handlers.py) rather
// than the frontend trying to guess.
const AUTO_DETECT_OPTION = { code: "auto", name: "Auto-detect" };

export default function App() {
  const [languages, setLanguages] = useState(FALLBACK_LANGUAGES);
  const [sourceLang, setSourceLang] = useState("en");
  const [targetLang, setTargetLang] = useState("az");
  const [microphoneId, setMicrophoneId] = useState("");
  const [outputId, setOutputId] = useState("");
  // Phase 9: independent of `muted` (which stops translation *audio* and,
  // below, also hides translated caption text) -- this hides the whole
  // transcript panel regardless of translation state.
  const [captionsOn, setCaptionsOn] = useState(true);

  const { microphones, outputs, permissionError } = useAudioDevices();
  const {
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
  } = useTranslationSession();

  const isRunning = status !== "idle" && status !== "error";
  const isPaused = status === "paused";
  const sourceLanguages = [AUTO_DETECT_OPTION, ...languages];

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
            label="I speak:"
            languages={sourceLanguages}
            value={sourceLang}
            onChange={setSourceLang}
            disabled={isRunning}
          />
          <LanguageSelector
            label="I want to hear:"
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

        <Controls
          isRunning={isRunning}
          isPaused={isPaused}
          onStart={handleStart}
          onStop={stop}
          onPause={pause}
          onResume={resume}
          startDisabled={!!permissionError}
        />

        <StatusIndicator status={status} errorMessage={errorMessage} />

        {/* Step 6 / Phase 9: translation on/off + captions on/off toggles,
            and volume sliders for both the translated audio and (new) an
            optional local mic monitor -- independent of the transcript
            display below. */}
        <AudioControls
          translationMuted={muted}
          onToggleTranslationMuted={setMuted}
          captionsOn={captionsOn}
          onToggleCaptions={setCaptionsOn}
          translationVolume={volume}
          onTranslationVolumeChange={setVolume}
          originalVolume={originalVolume}
          onOriginalVolumeChange={setOriginalVolume}
        />

        {/* Step 4: chat-style transcript history (final phrases, source +
            translation) plus a live line for the phrase still being
            spoken. Only finished phrases (history) are meant to be kept --
            the live partial is replaced in place and never accumulated.
            Phase 9: the whole panel is gated by "Captions" independent of
            translation mute; the translation line itself is additionally
            hidden while translation is muted (today mute only ever
            affected audio -- "Translation OFF" now hides the caption text
            too). */}
        {captionsOn && (
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
                {entry.translationText && !muted && (
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
        )}
      </div>
    </div>
  );
}

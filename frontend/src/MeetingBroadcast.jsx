import { useEffect, useRef, useState } from "react";
import DeviceSelector from "./components/DeviceSelector.jsx";
import { MicCapture } from "./audio/audioCapture.js";
import { useAudioDevices } from "./hooks/useAudioDevices.js";
import { AUDIO_SAMPLE_RATE, meetingIngestUrl } from "./config.js";

/**
 * Phase 11 (meeting broadcast mode) DEV-ONLY tool: a page that captures
 * your own microphone and streams it straight into a meeting's ingest
 * socket, exactly like _dev_stream_meeting_audio.py does from a WAV file
 * -- except live, from a real mic, so you can test the whole pipeline
 * (ingest -> transcribe -> translate -> broadcast -> listener) just by
 * talking, with no audio file to prepare.
 *
 * This is a stand-in for a real meeting bot (see the Phase 11 plan --
 * that's a separate, later decision) or for _dev_stream_meeting_audio.py
 * when a live back-and-forth is more convenient than a canned file.
 * `MeetingListener.jsx` is the other half -- open listener.html in
 * another tab/device to actually hear what this page sends.
 *
 * Deliberately reuses MicCapture (audio/audioCapture.js) completely
 * unmodified -- same worklet-based PCM16 capture the single-user app's
 * useWebSocket.js already uses -- and just points it at the meeting
 * ingest endpoint instead of /ws/translate. No pause/mute/volume here;
 * this page only ever does one thing: mic in, ingest socket out.
 */

export default function MeetingBroadcast() {
  const [meetingId, setMeetingId] = useState("dev-meeting");
  const [microphoneId, setMicrophoneId] = useState("");
  const [status, setStatus] = useState("idle"); // idle | connecting | broadcasting | error
  const [errorMessage, setErrorMessage] = useState("");

  const { microphones, permissionError } = useAudioDevices();
  const socketRef = useRef(null);
  const micRef = useRef(null);

  useEffect(() => {
    if (!microphoneId && microphones.length > 0) {
      setMicrophoneId(microphones[0].deviceId);
    }
  }, [microphones, microphoneId]);

  useEffect(() => {
    // Cleanup if the tab closes/navigates away mid-broadcast.
    return () => {
      micRef.current?.stop();
      socketRef.current?.close();
    };
  }, []);

  async function start() {
    if (!meetingId.trim()) {
      setErrorMessage("Enter a meeting ID first.");
      return;
    }
    setErrorMessage("");
    setStatus("connecting");

    const socket = new WebSocket(meetingIngestUrl(meetingId));
    socketRef.current = socket;

    socket.onopen = async () => {
      socket.send(JSON.stringify({ type: "start", sample_rate: AUDIO_SAMPLE_RATE }));
      try {
        const mic = new MicCapture({
          deviceId: microphoneId,
          onPCMChunk: (arrayBuffer) => {
            if (socketRef.current?.readyState === WebSocket.OPEN) {
              socketRef.current.send(arrayBuffer);
            }
          },
        });
        await mic.start();
        micRef.current = mic;
        setStatus("broadcasting");
      } catch (err) {
        setErrorMessage(err.message || "Could not start the microphone");
        setStatus("error");
        socket.close();
      }
    };

    socket.onmessage = (event) => {
      let msg;
      try {
        msg = JSON.parse(event.data);
      } catch {
        return;
      }
      if (msg.type === "error") {
        setErrorMessage(msg.message);
        setStatus("error");
      }
    };

    socket.onclose = () => {
      micRef.current?.stop();
      micRef.current = null;
      setStatus((prev) => (prev === "error" ? prev : "idle"));
    };

    socket.onerror = () => {
      // onclose fires right after and reports the terminal state.
    };
  }

  function stop() {
    micRef.current?.stop();
    micRef.current = null;
    if (socketRef.current?.readyState === WebSocket.OPEN) {
      socketRef.current.send(JSON.stringify({ type: "stop" }));
    }
    socketRef.current?.close();
    socketRef.current = null;
    setStatus("idle");
  }

  const isRunning = status === "connecting" || status === "broadcasting";

  return (
    <div className="app">
      <h1>Meeting Broadcast (dev)</h1>

      <div className="panel">
        <p className="transcript-empty">
          Dev-only tool: streams your mic into a meeting's ingest socket so you can test Phase 11 without preparing
          an audio file. Open listener.html in another tab to hear the translation.
        </p>

        <label className="field">
          <span className="field-label">Meeting ID</span>
          <input
            type="text"
            value={meetingId}
            onChange={(event) => setMeetingId(event.target.value)}
            disabled={isRunning}
            placeholder="e.g. dev-meeting"
          />
        </label>

        <DeviceSelector
          label="Microphone:"
          devices={microphones}
          value={microphoneId}
          onChange={setMicrophoneId}
          disabled={isRunning}
          emptyLabel="No microphones found"
        />

        {permissionError && <p className="permission-warning">Microphone access needed: {permissionError}</p>}

        <div className="controls">
          <button className="btn btn-start" onClick={start} disabled={isRunning || !!permissionError}>
            Start Broadcasting
          </button>
          <button className="btn btn-stop" onClick={stop} disabled={!isRunning}>
            Stop
          </button>
        </div>

        <div className="status-row">
          <span className={`status-dot status-${status === "broadcasting" ? "listening" : status}`} />
          <span>
            {status === "broadcasting"
              ? "Broadcasting -- speak into your mic"
              : status === "connecting"
                ? "Connecting..."
                : status === "error"
                  ? "Error"
                  : "Idle"}
          </span>
        </div>

        {errorMessage && <p className="status-error-detail">{errorMessage}</p>}
      </div>
    </div>
  );
}

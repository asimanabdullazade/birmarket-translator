import { useEffect, useRef, useState } from "react";
import { TranslationAudioPlayer } from "./audio/audioPlayback.js";
import { BACKEND_HTTP_URL, FALLBACK_LANGUAGES, meetingListenUrl } from "./config.js";
import { initTeamsPanel, isSidePanel } from "./teams/teamsPanel.js";

/**
 * Phase 11 (meeting broadcast mode): the companion page one meeting
 * attendee opens to hear the meeting translated live into their own
 * language -- see backend/websocket/meeting_handlers.py's
 * handle_meeting_listener for the server side of this connection.
 *
 * Deliberately minimal compared to App.jsx's full control panel: a
 * listener never captures or sends audio, never sends any control
 * message beyond the initial `?lang=` in the WebSocket URL, and picks
 * its language once per join (no in-socket language switching in v1 --
 * changing language means leaving and re-joining, which just opens a new
 * socket with a different `?lang=`). `TranslationAudioPlayer` is reused
 * completely unmodified from audio/audioPlayback.js -- same gapless,
 * non-overlapping playback queue the single-user app already uses for
 * Step 6.
 *
 * Reconnect here is a simple fixed delay, not useWebSocket.js's fuller
 * exponential-backoff logic -- there's no mic/session state to preserve
 * on this page, just "keep trying to be connected while joined."
 *
 * Phase 13 (Teams meeting side panel): this same component also renders
 * inside Teams, in a 320px-wide in-meeting side panel. Nothing about the
 * connection, protocol or playback changes -- Teams just loads this page
 * in an iframe. The only differences are cosmetic (a `teams-panel` class
 * that reflows for a narrow column and drops the page heading, which the
 * panel's own header already provides) plus the required TeamsJS
 * handshake in teams/teamsPanel.js. In a plain browser tab the Teams
 * probe fails silently and everything behaves exactly as in Phase 11,
 * which matters because the plain tab is how this page is developed.
 */

const RECONNECT_DELAY_MS = 2000;

function getMeetingIdFromUrl() {
  const params = new URLSearchParams(window.location.search);
  return params.get("meeting_id") || params.get("meetingId") || "";
}

export default function MeetingListener() {
  const [languages, setLanguages] = useState(FALLBACK_LANGUAGES);
  const [meetingId, setMeetingId] = useState(getMeetingIdFromUrl());
  const [lang, setLang] = useState("en");
  const [joined, setJoined] = useState(false);
  const [status, setStatus] = useState("idle"); // idle | connecting | listening | reconnecting | error
  const [errorMessage, setErrorMessage] = useState("");
  const [captions, setCaptions] = useState([]); // {key, kind: "transcript"|"translation", text}
  // Phase 13: null until the Teams probe settles, so the layout doesn't
  // flash full-width before collapsing into the panel.
  const [teamsState, setTeamsState] = useState(null);
  // Translation playback volume, 0..1. The ORIGINAL meeting audio is not
  // ours to control -- it is the Teams client playing through the
  // person's speakers, and this page is an iframe beside it with no
  // access to that stream. So this slider governs the translated voice
  // only, and the UI says so rather than implying a balance control it
  // cannot deliver.
  const [volume, setVolumeState] = useState(1);
  const [muted, setMutedState] = useState(false);

  const playerRef = useRef(null);
  const socketRef = useRef(null);
  const reconnectTimeoutRef = useRef(null);
  const joinedRef = useRef(false);
  const captionsRef = useRef(null);
  // Every caption for this session, uncapped. `captions` state is
  // deliberately trimmed to the last 50 so the DOM stays small, but a
  // transcript that silently dropped everything before the last 50 lines
  // would be worse than no transcript at all.
  const historyRef = useRef([]);

  useEffect(() => {
    // Resolves either way and never throws -- see teams/teamsPanel.js.
    initTeamsPanel().then(setTeamsState);
  }, []);

  useEffect(() => {
    fetch(`${BACKEND_HTTP_URL}/languages`)
      .then((res) => res.json())
      .then((data) => Array.isArray(data) && data.length > 0 && setLanguages(data))
      .catch(() => {
        // Backend not reachable yet -- keep the fallback list.
      });
  }, []);

  useEffect(() => {
    if (captionsRef.current) {
      captionsRef.current.scrollTop = captionsRef.current.scrollHeight;
    }
  }, [captions]);

  useEffect(() => {
    // Cleanup on unmount -- leave() below handles the normal "Leave"
    // button path; this covers navigating away/closing the tab.
    return () => {
      joinedRef.current = false;
      if (reconnectTimeoutRef.current) clearTimeout(reconnectTimeoutRef.current);
      socketRef.current?.close();
      playerRef.current?.stop();
    };
  }, []);

  function setVolume(next) {
    setVolumeState(next);
    playerRef.current?.setVolume(next);
  }

  function toggleMuted() {
    const next = !muted;
    setMutedState(next);
    playerRef.current?.setMuted(next);
  }

  function connect() {
    if (!playerRef.current) {
      playerRef.current = new TranslationAudioPlayer();
      // The player is created on first join, after the user may already
      // have moved the slider -- carry the current settings over rather
      // than silently resetting them to full volume.
      playerRef.current.setVolume(volume);
      playerRef.current.setMuted(muted);
    }
    // Reuses the "reconnecting" status/style for the initial connection
    // attempt too -- both are "trying to connect," and it saves adding a
    // separate "connecting" CSS class that would look identical anyway.
    setStatus("reconnecting");
    setErrorMessage("");

    const socket = new WebSocket(meetingListenUrl(meetingId, lang));
    socketRef.current = socket;

    socket.onmessage = (event) => {
      let msg;
      try {
        msg = JSON.parse(event.data);
      } catch {
        return;
      }
      if (msg.type === "transcript" || msg.type === "translation") {
        historyRef.current.push({
          timestamp: msg.timestamp,
          speaker: msg.speaker || null,
          kind: msg.type,
          text: msg.text,
          lang: msg.type === "translation" ? msg.target_lang : msg.detected_language || null,
        });
      }

      if (msg.type === "status") {
        setStatus(msg.status === "listening" ? "listening" : msg.status);
      } else if (msg.type === "transcript") {
        setCaptions((prev) => [
          ...prev.slice(-49),
          { key: `${msg.timestamp}-t`, kind: "transcript", text: msg.text, speaker: msg.speaker || null },
        ]);
      } else if (msg.type === "translation") {
        setCaptions((prev) => [
          ...prev.slice(-49),
          { key: `${msg.timestamp}-x`, kind: "translation", text: msg.text, speaker: msg.speaker || null },
        ]);
      } else if (msg.type === "audio") {
        playerRef.current.enqueue(msg.audio_base64);
      } else if (msg.type === "error") {
        setErrorMessage(msg.message);
      }
    };

    socket.onclose = () => {
      if (!joinedRef.current) return; // a deliberate leave() -- don't reconnect
      setStatus("reconnecting");
      reconnectTimeoutRef.current = setTimeout(connect, RECONNECT_DELAY_MS);
    };

    socket.onerror = () => {
      // onclose fires right after and decides reconnect vs. not -- avoid
      // duplicating that decision here.
    };
  }

  function join() {
    if (!meetingId.trim()) {
      setErrorMessage("Enter a meeting ID first.");
      return;
    }
    joinedRef.current = true;
    setJoined(true);
    setCaptions([]);
    historyRef.current = [];
    connect();
  }

  function downloadTranscript() {
    const entries = historyRef.current;
    if (entries.length === 0) return;

    const header = [
      `Meeting: ${meetingId}`,
      `Listening in: ${lang}`,
      `Exported: ${new Date().toISOString()}`,
      `Lines: ${entries.length}`,
      "",
      "".padEnd(60, "-"),
      "",
    ];

    const body = entries.map((entry) => {
      // Times are ISO-8601 UTC on the wire; show local clock time, which
      // is what someone reading the transcript afterwards actually wants.
      let clock = "";
      try {
        clock = new Date(entry.timestamp).toLocaleTimeString();
      } catch {
        clock = entry.timestamp || "";
      }
      const who = entry.speaker ? ` ${entry.speaker}` : "";
      const tag = entry.kind === "translation" ? `[${entry.lang || "translation"}]` : "[original]";
      return `${clock}${who} ${tag}\n${entry.text}\n`;
    });

    const blob = new Blob([header.concat(body).join("\n")], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `transcript-${meetingId}-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.txt`;
    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);
    // Revoke on the next tick -- revoking synchronously can cancel the
    // download in some browsers before it has started reading the blob.
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function leave() {
    joinedRef.current = false;
    setJoined(false);
    setStatus("idle");
    if (reconnectTimeoutRef.current) clearTimeout(reconnectTimeoutRef.current);
    socketRef.current?.close();
    playerRef.current?.stop();
  }

  const inPanel = isSidePanel(teamsState);

  return (
    <div className={inPanel ? "app teams-panel" : "app"}>
      {/* The Teams side panel draws its own header with the tab name, so a
          second in-page title just eats vertical space in a 320px column. */}
      {!inPanel && <h1>Live Meeting Translation</h1>}

      <div className="panel">
        {!joined && (
          <>
            {/* Inside Teams the meeting_id is fixed by the manifest's
                contentUrl (see src/teamsConfig.js), so showing an editable
                field would just invite someone to break their own panel. */}
            {!inPanel && (
              <div className="field">
                <span className="field-label">Meeting ID</span>
                <input
                  type="text"
                  value={meetingId}
                  onChange={(event) => setMeetingId(event.target.value)}
                  placeholder="e.g. weekly-standup"
                />
              </div>
            )}

            <div className="field">
              <span className="field-label">I want to hear:</span>
              <select value={lang} onChange={(event) => setLang(event.target.value)}>
                {languages.map((language) => (
                  <option key={language.code} value={language.code}>
                    {language.name}
                  </option>
                ))}
              </select>
            </div>

            <div className="controls">
              <button className="btn btn-start" onClick={join}>
                Join
              </button>
            </div>
          </>
        )}

        {joined && (
          <>
            <div className="status-row">
              <span className={`status-dot status-${status}`} />
              <span>{status === "listening" ? "Listening" : status === "reconnecting" ? "Connecting..." : status}</span>
            </div>

            {errorMessage && <p className="status-error-detail">{errorMessage}</p>}

            <div className="field volume-field">
              <span className="field-label">
                Translation volume{muted ? " (muted)" : ` (${Math.round(volume * 100)}%)`}
              </span>
              <input
                type="range"
                min="0"
                max="100"
                value={Math.round(volume * 100)}
                disabled={muted}
                onChange={(event) => setVolume(Number(event.target.value) / 100)}
              />
            </div>

            <p className="volume-hint">
              This controls the translated voice only. To hear more or less of the original
              speakers, use Teams&apos; own volume.
            </p>

            <div className="controls">
              <button className="btn btn-secondary" onClick={toggleMuted}>
                {muted ? "Unmute translation" : "Mute translation"}
              </button>
              <button
                className="btn btn-secondary"
                onClick={downloadTranscript}
                disabled={captions.length === 0}
              >
                Download transcript
              </button>
              <button className="btn btn-stop" onClick={leave}>
                Leave
              </button>
            </div>

            <div className="transcript-panel" ref={captionsRef}>
              {captions.length === 0 && <p className="transcript-empty">Captions will appear here once someone speaks.</p>}
              {captions.map((caption) => (
                <div className="transcript-entry" key={caption.key}>
                  {/* Phase 14: attribution is best-effort (see
                      bot/speaker_tracker.js) -- when the bot can't tell who
                      spoke, the caption renders exactly as it did before
                      rather than showing a placeholder name. */}
                  {caption.speaker && <div className="caption-speaker">{caption.speaker}</div>}
                  <div className={caption.kind === "translation" ? "translation-line" : "transcript-line"}>
                    {caption.text}
                  </div>
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

"""
Pydantic models for the WebSocket message protocol between the React
frontend and the FastAPI backend.

Wire format
-----------
The client sends two kinds of frames on the same socket:
  1. JSON text frames for control messages -> parsed as one of the
     `Client*Message` models below via `parse_client_message`.
  2. Raw binary frames containing PCM16LE mono audio samples, sent only
     while a session is active (between a "start" and a "stop" message).

The server only ever sends JSON text frames, all shaped like
`ServerMessage` (a discriminated union on `type`).
"""

from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field, TypeAdapter

# --- Status enum, mirrors the four states the UI shows ---

StatusValue = Literal["connected", "listening", "translating", "error"]


# --- Client -> server control messages ---


class StartMessage(BaseModel):
    type: Literal["start"] = "start"
    source_lang: str
    target_lang: str
    sample_rate: int = 16000


class StopMessage(BaseModel):
    type: Literal["stop"] = "stop"


class SetMutedMessage(BaseModel):
    """Toggle whether the server should bother synthesizing translated
    speech at all (Step 6) -- sent whenever the user clicks the mute
    button, not just at session start, so it can be flipped mid-
    conversation. This is a server-side cost/bandwidth optimization (skip
    calling the TTS provider entirely while muted); it has nothing to do
    with the client's own volume/mute *playback* control (see
    frontend/src/audio/audioPlayback.js), which works independently on
    whatever audio has already been received."""

    type: Literal["set_muted"] = "set_muted"
    muted: bool


class AudioPlayedMessage(BaseModel):
    """Step 7 (latency measurement): sent by the client the instant it
    actually begins playing the *first* synthesized audio chunk for a
    given phrase (see frontend/src/hooks/useWebSocket.js and
    TranslationAudioPlayer._enqueueOne in frontend/src/audio/
    audioPlayback.js). This is the one point in the whole speech-start-to-
    audio-heard timeline that can only be known client-side -- everything
    else (VAD detection, transcript/translation/TTS completion) already
    happens on the server. `played_at_ms` is the client's own epoch-
    millisecond wall clock (JS `Date.now()`), adjusted for any Web Audio
    scheduling delay still ahead of it. `backend/websocket/handlers.py`
    compares this directly against its own UTC timestamps to log a full
    latency breakdown -- see "Measuring latency" in the README for why
    that's valid only because the frontend and backend run on the same
    machine's clock in this project's current dev setup."""

    type: Literal["audio_played"] = "audio_played"
    timestamp: str
    played_at_ms: float


ClientMessage = Annotated[
    Union[StartMessage, StopMessage, SetMutedMessage, AudioPlayedMessage], Field(discriminator="type")
]

_client_message_adapter: TypeAdapter[ClientMessage] = TypeAdapter(ClientMessage)


def parse_client_message(raw: str) -> ClientMessage:
    """Parse a JSON text frame received from the client into a typed message."""
    return _client_message_adapter.validate_json(raw)


# --- Server -> client messages ---


class StatusMessage(BaseModel):
    type: Literal["status"] = "status"
    status: StatusValue
    detail: Optional[str] = None


class TranscriptMessage(BaseModel):
    """The source-language speech-to-text result (interim or final).

    is_final=False ("partial") means speech is still ongoing and this text
    may still grow or change -- the client should show it but not treat it
    as settled. is_final=True means the phrase has ended; this is the only
    version that should be kept/stored (see Step 4: "Only the final version
    should eventually be stored")."""

    type: Literal["transcript"] = "transcript"
    text: str
    is_final: bool
    # ISO-8601 UTC timestamp of when the *phrase* started (not when this
    # message was sent) -- shared by every partial and the final message
    # for the same phrase, so a client can group them and/or show when the
    # person actually started speaking rather than when processing finished.
    timestamp: str
    # Only ever set on a final transcript, and only by providers that
    # support it (see TranslationEvent.detected_language in
    # backend/translation/base.py). Informational only.
    detected_language: Optional[str] = None


class TranslationMessage(BaseModel):
    """The translated text corresponding to a transcript segment.

    is_final=True is the authoritative translation of a completed phrase
    (as before Phase 8). is_final=False is a *growing* incremental
    translation sent while the phrase is still being spoken (Phase 8's
    streaming translation, gemini/mock providers only -- see "Streaming
    translation" in backend/websocket/handlers.py's docstring) -- `text`
    is always the FULL accumulated translation committed so far, not just
    the newest fragment, so a client can render it the same way it already
    renders a growing partial transcript (overwrite in place)."""

    type: Literal["translation"] = "translation"
    text: str
    is_final: bool
    source_lang: str
    target_lang: str
    timestamp: str


class AudioMessage(BaseModel):
    """One chunk of synthesized speech audio for a translated phrase
    (Step 6) -- always derived from a *final* translation (see
    TranslationProvider.synthesize_speech in backend/translation/base.py).
    `audio_base64` is a complete, independently-decodable WAV file (see
    _pcm16_to_wav_bytes in backend/websocket/handlers.py), not a raw PCM
    fragment, so the client can hand it straight to the browser's
    decodeAudioData. A single phrase's speech may arrive as several of
    these in a row -- one per speakable chunk (see
    backend/translation/text_chunking.py) -- streamed as each chunk
    finishes synthesizing rather than batched, so playback of the first
    chunk can start before later ones are ready. The client is expected to
    queue and play them back to back, never overlapping (see
    frontend/src/audio/audioPlayback.js)."""

    type: Literal["audio"] = "audio"
    audio_base64: str
    sample_rate: int
    timestamp: str


class ErrorMessage(BaseModel):
    type: Literal["error"] = "error"
    message: str


ServerMessage = Union[
    StatusMessage, TranscriptMessage, TranslationMessage, AudioMessage, ErrorMessage
]

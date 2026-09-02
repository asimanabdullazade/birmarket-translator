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


ClientMessage = Annotated[Union[StartMessage, StopMessage], Field(discriminator="type")]

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
    """The translated text corresponding to a transcript segment. Always
    final -- only completed phrases are translated (see base.py)."""

    type: Literal["translation"] = "translation"
    text: str
    is_final: bool
    source_lang: str
    target_lang: str
    timestamp: str


class ErrorMessage(BaseModel):
    type: Literal["error"] = "error"
    message: str


ServerMessage = Union[StatusMessage, TranscriptMessage, TranslationMessage, ErrorMessage]

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
    """The source-language speech-to-text result (interim or final)."""

    type: Literal["transcript"] = "transcript"
    text: str
    is_final: bool


class TranslationMessage(BaseModel):
    """The translated text corresponding to a transcript segment."""

    type: Literal["translation"] = "translation"
    text: str
    is_final: bool
    source_lang: str
    target_lang: str


class ErrorMessage(BaseModel):
    type: Literal["error"] = "error"
    message: str


ServerMessage = Union[StatusMessage, TranscriptMessage, TranslationMessage, ErrorMessage]

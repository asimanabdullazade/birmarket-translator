"""
Provider-agnostic interface for real-time speech translation.

Every real-time speech translation API (OpenAI Realtime, Azure Speech
Translation, Google STT + Cloud Translation, ...) is shaped roughly the
same way once you abstract over vendor SDKs: you open a session for a
(source_lang, target_lang) pair, stream it raw audio, and get back a
sequence of transcript/translation events, some interim and some final.

`TranslationProvider` captures exactly that shape. `websocket/handlers.py`
only ever talks to this interface, so swapping providers is a one-line
change in `factory.py` plus a new subclass here -- nothing else in the app
needs to know which vendor is behind it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum


class EventKind(str, Enum):
    TRANSCRIPT = "transcript"  # source-language speech-to-text
    TRANSLATION = "translation"  # translated text


@dataclass
class TranslationEvent:
    kind: EventKind
    text: str
    is_final: bool


class TranslationProvider(ABC):
    """Base class every translation backend (mock or real) must implement."""

    @abstractmethod
    async def start_session(self, source_lang: str, target_lang: str) -> None:
        """Open whatever session/connection the provider needs for this pair of languages."""

    @abstractmethod
    async def process_audio_chunk(self, pcm16_bytes: bytes) -> list[TranslationEvent]:
        """
        Feed one chunk of raw PCM16LE mono audio to the provider and return
        any transcript/translation events it produced as a result. May
        return an empty list if the provider is still buffering internally.
        """

    @abstractmethod
    async def close_session(self) -> list[TranslationEvent]:
        """
        End the session (e.g. flush a final partial chunk) and return any
        last events, such as the final finalized transcript/translation.
        """

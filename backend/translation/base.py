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
from typing import AsyncIterator, Optional


class EventKind(str, Enum):
    TRANSCRIPT = "transcript"  # source-language speech-to-text
    TRANSLATION = "translation"  # translated text


@dataclass
class TranslationEvent:
    kind: EventKind
    text: str
    is_final: bool
    # Only meaningful on TRANSCRIPT events, and only some providers fill it
    # in (see gemini_provider.py) -- the language the provider itself
    # detected the audio to be in, independent of the source_lang the user
    # selected in the UI. Purely informational; never used to override the
    # user's selection.
    detected_language: Optional[str] = None


class TranslationProvider(ABC):
    """Base class every translation backend (mock or real) must implement."""

    @abstractmethod
    async def start_session(self, source_lang: str, target_lang: str) -> None:
        """Open whatever session/connection the provider needs for this pair of languages."""

    @abstractmethod
    async def process_audio_chunk(self, pcm16_bytes: bytes) -> list[TranslationEvent]:
        """
        Feed one *complete* utterance of raw PCM16LE mono audio to the
        provider and return the transcript/translation events it produced --
        always treat these as final (is_final=True). May return an empty
        list if nothing intelligible was heard.
        """

    async def transcribe_partial(self, pcm16_bytes: bytes) -> Optional[str]:
        """
        Best-effort interim transcript for a not-yet-finished utterance (the
        audio captured so far, growing as speech continues). Returns None if
        unsupported, or if there isn't enough audio yet / nothing
        intelligible was heard. Default: unsupported -- providers opt in by
        overriding this.

        Unlike process_audio_chunk, this must NOT translate -- partial
        results are for live transcript feedback only; only a *final*
        utterance gets translated. Keep this fast/cheap where possible,
        since it may be called several times per utterance as speech
        continues (see VAD_PARTIAL_INTERVAL_MS in config/settings.py).
        """
        return None

    async def synthesize_speech(self, text: str) -> AsyncIterator[tuple[bytes, int]]:
        """
        Convert already-*translated* text to speech (Step 6), yielding
        (pcm16le_mono_audio_bytes, sample_rate) tuples -- one per speakable
        chunk (see text_chunking.split_for_speech) -- as soon as each is
        ready. `websocket/handlers.py` streams each chunk to the client
        immediately as it's yielded, rather than waiting for the whole
        phrase, so playback of the first chunk can start before later
        chunks (or even later words in the same sentence) have finished
        synthesizing -- see "Start playback before the entire sentence is
        generated" in Step 6.

        Only ever called with a *final* translation -- there's no such
        thing as speaking a partial/still-changing translation aloud (and
        partial results are never translated in the first place, see
        transcribe_partial above).

        Default: unsupported -- yields nothing. Providers opt in by
        overriding this (see gemini_provider.py; local_provider.py
        deliberately does not -- see the note in that file for why).
        """
        return
        yield b"", 0  # pragma: no cover -- unreachable; makes this an async generator

    @abstractmethod
    async def close_session(self) -> list[TranslationEvent]:
        """
        End the session (e.g. flush a final partial chunk) and return any
        last events, such as the final finalized transcript/translation.
        """

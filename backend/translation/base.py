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
class Transcription:
    """Phase 11 (meeting broadcast mode): the result of a one-shot,
    auto-detected transcription -- see TranslationProvider.transcribe_final
    below for why this is a separate method/return type from
    process_audio_chunk rather than reusing TranslationEvent."""

    text: str
    detected_language: Optional[str]


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
    # Step 7 (latency measurement): ISO-8601 UTC timestamp of when THIS
    # specific event's result actually became available, for providers
    # that can distinguish sub-steps -- see local_provider.py, whose
    # transcribe and translate calls are genuinely sequential. Leave this
    # unset (None) if your provider produces transcript+translation
    # atomically in one call (gemini_provider.py's single Interactions API
    # round trip; mock_provider.py) -- `websocket/handlers.py` then falls
    # back to the instant process_audio_chunk() returned for both events,
    # which correctly reports a ~zero "Translation" leg for an atomic
    # provider instead of fabricating a non-zero one that isn't real.
    generated_at: Optional[str] = None


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

    async def translate_partial(self, new_stable_text: str, already_committed_translation: str) -> Optional[str]:
        """
        Phase 8 (streaming translation): translate a newly-*stabilized*
        fragment of source text -- one `websocket/handlers.py` has decided,
        via a word-level "local agreement" policy over consecutive
        transcribe_partial() results (see backend/translation/stability.py),
        is unlikely to change -- as a coherent CONTINUATION of whatever's
        already been committed/translated/spoken for this same phrase.

        `already_committed_translation` is the full translation text
        already committed so far (may be empty, for the first commit of a
        phrase); `new_stable_text` is only the newly-stabilized source-text
        increment, never audio and never the whole phrase. Return ONLY the
        new continuation -- never a retranslation of
        already_committed_translation -- so callers can safely append your
        result and never re-synthesize/re-speak text that's already been
        spoken (see "avoid repeating audio" in the README's "Using
        streaming translation" section).

        Default: unsupported (None) -- providers that don't override this
        simply never produce incremental commits, and every phrase for them
        behaves exactly as it did before Phase 8 (wait for the full
        utterance). This must stay fast/cheap-ish for the same reason
        transcribe_partial does: it can be called several times per phrase.
        """
        return None

    async def transcribe_final(self, pcm16_bytes: bytes) -> Optional[Transcription]:
        """
        Phase 11 (meeting broadcast mode): one-shot transcript of a
        *complete* utterance, auto-detecting the spoken language, with NO
        translation. Exists so meeting_handlers.py can transcribe an
        utterance exactly ONCE regardless of how many target languages it
        ends up translating that transcript into (contrast
        process_audio_chunk, which is bound to one fixed (source_lang,
        target_lang) pair for the whole session via start_session and
        transcribes+translates atomically -- not a fit here, since a
        meeting's speaker language isn't known upfront and one utterance
        may need translating into up to two different targets).

        Default: unsupported (None) -- meeting mode requires this to be
        implemented; providers that don't override it simply cannot be used
        as a meeting ingest provider (see the provider allowlist in
        meeting_handlers.py).
        """
        return None

    async def translate_final(self, text: str, source_lang: str, target_lang: str) -> Optional[str]:
        """
        Phase 11 (meeting broadcast mode): standalone translation of
        already-final text into an explicit target_lang -- NOT a
        continuation (contrast translate_partial, which has no
        source_lang/target_lang params because it continues whatever this
        bound instance is already mid-translating for its one fixed
        session pair). source_lang/target_lang are passed per call so one
        provider instance can translate the same transcript into several
        different targets in turn.

        Default: unsupported (None).
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

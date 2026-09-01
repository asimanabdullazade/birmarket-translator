"""
Mock translation provider.

Produces deterministic, plausible-looking transcript/translation events
from audio energy alone -- no network calls, no API key. This is the
default provider (see config/settings.py: TRANSLATION_PROVIDER=mock) so the
whole app -- frontend, WebSocket protocol, audio pipeline -- is runnable
and demoable with zero credentials. Swap in a real provider from this same
interface (see base.py) once you have API access.
"""

from __future__ import annotations

from backend.audio.vad import is_speech
from backend.translation.base import EventKind, TranslationEvent, TranslationProvider


class MockTranslationProvider(TranslationProvider):
    def __init__(self) -> None:
        self._source_lang = "en"
        self._target_lang = "az"
        self._chunk_count = 0

    async def start_session(self, source_lang: str, target_lang: str) -> None:
        self._source_lang = source_lang
        self._target_lang = target_lang
        self._chunk_count = 0

    async def process_audio_chunk(self, pcm16_bytes: bytes) -> list[TranslationEvent]:
        self._chunk_count += 1

        if not is_speech(pcm16_bytes):
            return []

        transcript_text = f"[mock transcript #{self._chunk_count}] ({self._source_lang})"
        translation_text = (
            f"[mock translation #{self._chunk_count}] ({self._target_lang})"
        )

        return [
            TranslationEvent(kind=EventKind.TRANSCRIPT, text=transcript_text, is_final=True),
            TranslationEvent(kind=EventKind.TRANSLATION, text=translation_text, is_final=True),
        ]

    async def close_session(self) -> list[TranslationEvent]:
        return []

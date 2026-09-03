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

import math
import struct
from typing import AsyncIterator, Optional

from backend.audio.vad import is_speech
from backend.translation.base import EventKind, TranslationEvent, TranslationProvider
from backend.translation.text_chunking import split_for_speech

# Sample rate for the placeholder "speech" this provider synthesizes --
# arbitrary (there's no real TTS engine here), just needs to match whatever
# we tell the client in the AudioMessage.
_TTS_SAMPLE_RATE = 16000


def _beep_pcm16(duration_s: float = 0.3, freq_hz: float = 440.0) -> bytes:
    """A short sine-wave beep, PCM16LE mono -- a deterministic, zero-
    dependency stand-in for real synthesized speech. Just enough to
    exercise the streaming/queueing/no-overlap/volume/mute plumbing from
    Step 6 end to end without any TTS engine or API key -- see "Testing
    TTS" in the README."""
    n_samples = int(_TTS_SAMPLE_RATE * duration_s)
    samples = [
        int(0.2 * 32767 * math.sin(2 * math.pi * freq_hz * i / _TTS_SAMPLE_RATE))
        for i in range(n_samples)
    ]
    return struct.pack(f"<{n_samples}h", *samples)


class MockTranslationProvider(TranslationProvider):
    def __init__(self) -> None:
        self._source_lang = "en"
        self._target_lang = "az"
        self._chunk_count = 0
        self._partial_count = 0

    async def start_session(self, source_lang: str, target_lang: str) -> None:
        self._source_lang = source_lang
        self._target_lang = target_lang
        self._chunk_count = 0
        self._partial_count = 0

    async def transcribe_partial(self, pcm16_bytes: bytes) -> Optional[str]:
        # Deliberately supported here (unlike most providers, where it's
        # optional) so Step 4's partial/final plumbing can be exercised with
        # zero external dependencies -- see "Testing STT" in the README.
        self._partial_count += 1
        return f"[mock partial #{self._partial_count}] ({self._source_lang})"

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

    async def synthesize_speech(self, text: str) -> AsyncIterator[tuple[bytes, int]]:
        # One beep per speakable chunk (see text_chunking.split_for_speech),
        # each at a slightly different pitch purely so you can audibly tell
        # chunks apart while testing the queue/no-overlap behavior -- see
        # "Testing TTS" in the README.
        for i, _chunk in enumerate(split_for_speech(text)):
            yield _beep_pcm16(freq_hz=440.0 + 80.0 * i), _TTS_SAMPLE_RATE

    async def close_session(self) -> list[TranslationEvent]:
        return []

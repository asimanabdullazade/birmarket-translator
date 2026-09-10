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
from backend.translation.base import EventKind, Transcription, TranslationEvent, TranslationProvider
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
        self._partial_translation_count = 0
        # Phase 11 (meeting broadcast mode): settable by test harnesses
        # (e.g. _verify_meeting_handlers.py's CountingProvider-adjacent
        # scenarios) to simulate a speaker in whichever language a test
        # wants, without a constructor change. Defaults to "en" purely as
        # a stable, predictable default -- not a claim about what a real
        # speaker would say.
        self._mock_detected_language = "en"
        self._final_transcribe_count = 0
        self._final_translate_count = 0

    async def start_session(self, source_lang: str, target_lang: str) -> None:
        self._source_lang = source_lang
        self._target_lang = target_lang
        self._chunk_count = 0
        self._partial_count = 0
        self._partial_translation_count = 0

    async def transcribe_partial(self, pcm16_bytes: bytes) -> Optional[str]:
        # Deliberately supported here (unlike most providers, where it's
        # optional) so Step 4's partial/final plumbing can be exercised with
        # zero external dependencies -- see "Testing STT" in the README.
        self._partial_count += 1
        return f"[mock partial #{self._partial_count}] ({self._source_lang})"

    async def translate_partial(self, new_stable_text: str, already_committed_translation: str) -> Optional[str]:
        # Deliberately supported here too (Phase 8), same reasoning as
        # transcribe_partial above -- so the whole incremental-commit
        # pipeline is exercisable with zero external dependencies. Echoes
        # the new fragment back with a counter so a test/manual run can
        # visibly confirm this is only ever called with genuinely NEW text,
        # never a re-send of already_committed_translation -- see "Using
        # streaming translation" in the README.
        self._partial_translation_count += 1
        return f"[mock translation Δ{self._partial_translation_count}] ({self._target_lang}): {new_stable_text}"

    async def transcribe_final(self, pcm16_bytes: bytes) -> Optional[Transcription]:
        # Phase 11: deliberately supported (unlike most providers) so the
        # meeting-broadcast pipeline is fully testable with zero external
        # dependencies -- see "Testing meeting broadcast mode" in the
        # README. Auto-detects nothing for real; just reports whatever
        # self._mock_detected_language is currently set to.
        if not is_speech(pcm16_bytes):
            return None
        self._final_transcribe_count += 1
        transcript_text = f"[mock final transcript #{self._final_transcribe_count}] ({self._mock_detected_language})"
        return Transcription(text=transcript_text, detected_language=self._mock_detected_language)

    async def translate_final(self, text: str, source_lang: str, target_lang: str) -> Optional[str]:
        self._final_translate_count += 1
        return f"[{target_lang}] {text}"

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

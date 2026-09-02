"""
Voice Activity Detection: turns a continuous stream of small audio frames
into discrete speech phrases ("utterances") ready for translation, instead
of translating fixed-size time windows regardless of whether they contain
speech or silence.

State machine
-------------
    silence -> speech starts -> speech continues -> speech stops -> silence

- SILENCE: incoming frames are kept in a short rolling "pre-speech" buffer
  rather than discarded. Without this, by the time enough energy has
  accumulated to *detect* speech, the first word (or its leading
  consonant) would already be gone -- the pre-speech buffer is what gets
  prepended to the utterance so the front of the phrase isn't clipped.
- SPEECH: frames accumulate into the current utterance. A single silent
  frame doesn't immediately end it -- brief mid-sentence pauses are normal
  ("...this is... a test") and cutting on the very first quiet moment
  would fragment every phrase into pieces. Silence has to persist for
  `end_silence_ms` (recommended 400-700ms) before the utterance is
  considered finished.

Frame-level speech/silence classification is delegated to `is_speech()` in
vad.py (simple RMS-energy-over-threshold); this module is the stateful
layer on top of it that decides *when a phrase begins and ends*.

Usage: one `SpeechSegmenter` per session. Feed every incoming audio frame
to `push()`, which returns zero or more `SegmenterEvent`s (a SPEECH_START
when a phrase begins, an UTTERANCE_READY carrying the full phrase's audio
once enough trailing silence has elapsed). Call `flush()` when the stream
ends (e.g. on `stop`) to recover whatever phrase was still in progress.

Frames may be any length (each incoming WebSocket frame's actual duration
is computed from its byte length, sample rate, and channel count) --
nothing here assumes a fixed frame size.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Deque, List, Optional, Tuple

from backend.audio.vad import is_speech

_BYTES_PER_SAMPLE = 2  # PCM16


class SegmenterEventKind(Enum):
    SPEECH_START = "speech_start"
    UTTERANCE_READY = "utterance_ready"


@dataclass
class SegmenterEvent:
    kind: SegmenterEventKind
    audio: Optional[bytes] = None  # populated only for UTTERANCE_READY


class SpeechSegmenter:
    """Stateful VAD: silence -> speech -> silence, one instance per session."""

    def __init__(
        self,
        sample_rate: int,
        channels: int = 1,
        pre_speech_ms: float = 400.0,
        end_silence_ms: float = 500.0,
        vad_threshold: float = 0.01,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.pre_speech_ms = pre_speech_ms
        self.end_silence_ms = end_silence_ms
        self.vad_threshold = vad_threshold

        # Rolling window of (frame, duration_ms) kept while silent, capped
        # by total duration rather than frame count (frames may vary in
        # length) -- this is the pre-speech lookback.
        self._pre_speech_buffer: Deque[Tuple[bytes, float]] = deque()
        self._pre_speech_total_ms = 0.0

        self._speaking = False
        self._utterance = bytearray()
        self._silence_run_ms = 0.0

    def _duration_ms(self, frame: bytes) -> float:
        samples = len(frame) / _BYTES_PER_SAMPLE / self.channels
        return samples / self.sample_rate * 1000.0

    def push(self, frame: bytes) -> List[SegmenterEvent]:
        events: List[SegmenterEvent] = []
        duration_ms = self._duration_ms(frame)
        speech_frame = is_speech(frame, threshold=self.vad_threshold)

        if not self._speaking:
            if speech_frame:
                self._speaking = True
                self._silence_run_ms = 0.0
                self._utterance = bytearray()
                # Prepend whatever we'd been holding onto during silence --
                # this is what keeps the first word from being clipped.
                for buffered_frame, _ in self._pre_speech_buffer:
                    self._utterance.extend(buffered_frame)
                self._pre_speech_buffer.clear()
                self._pre_speech_total_ms = 0.0
                self._utterance.extend(frame)
                events.append(SegmenterEvent(kind=SegmenterEventKind.SPEECH_START))
            else:
                self._pre_speech_buffer.append((frame, duration_ms))
                self._pre_speech_total_ms += duration_ms
                while self._pre_speech_total_ms > self.pre_speech_ms and len(self._pre_speech_buffer) > 1:
                    _, popped_ms = self._pre_speech_buffer.popleft()
                    self._pre_speech_total_ms -= popped_ms
            return events

        # Currently in a speech segment.
        self._utterance.extend(frame)
        if speech_frame:
            self._silence_run_ms = 0.0
        else:
            self._silence_run_ms += duration_ms
            if self._silence_run_ms >= self.end_silence_ms:
                events.append(
                    SegmenterEvent(kind=SegmenterEventKind.UTTERANCE_READY, audio=bytes(self._utterance))
                )
                self._speaking = False
                self._utterance = bytearray()
                self._silence_run_ms = 0.0
                self._pre_speech_buffer.clear()
                self._pre_speech_total_ms = 0.0
        return events

    def flush(self) -> Optional[bytes]:
        """Call when the stream ends. Returns whatever phrase was still in
        progress (the user was speaking when `stop` arrived, before the
        end-of-speech silence threshold had a chance to elapse), or None if
        nothing was in progress."""
        if self._speaking and self._utterance:
            audio = bytes(self._utterance)
        else:
            audio = None
        self._speaking = False
        self._utterance = bytearray()
        self._silence_run_ms = 0.0
        self._pre_speech_buffer.clear()
        self._pre_speech_total_ms = 0.0
        return audio

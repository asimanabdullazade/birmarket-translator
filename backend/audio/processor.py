"""
Server-side audio buffering utilities.

`AudioBuffer`'s fixed-duration chunking (`pop_ready_chunks`/`flush`) is no
longer used by the WebSocket handler -- audio is now split into phrases by
detected speech instead, via `backend.audio.segmenter.SpeechSegmenter`,
which reacts to actual speech/silence rather than translating arbitrary
time windows regardless of content. `AudioBuffer.to_numpy` is still used
directly by translation providers (e.g. backend/translation/local_provider.py)
to convert raw PCM16LE bytes into a float32 array, so the class is kept for
that; the chunking methods remain here in case a fixed-window mode is ever
useful again (e.g. a future provider that genuinely wants uniform chunks).
"""

from __future__ import annotations

import numpy as np


class AudioBuffer:
    """Accumulates PCM16LE bytes and yields fixed-duration chunks."""

    def __init__(self, sample_rate: int, channels: int, chunk_seconds: float) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.chunk_seconds = chunk_seconds
        self._bytes_per_sample = 2  # PCM16
        self._chunk_size_bytes = int(
            sample_rate * channels * self._bytes_per_sample * chunk_seconds
        )
        self._buffer = bytearray()

    def add(self, data: bytes) -> None:
        self._buffer.extend(data)

    def pop_ready_chunks(self) -> list[bytes]:
        """Return as many complete chunks as are currently available, removing them from the buffer."""
        chunks: list[bytes] = []
        while len(self._buffer) >= self._chunk_size_bytes:
            chunk = bytes(self._buffer[: self._chunk_size_bytes])
            del self._buffer[: self._chunk_size_bytes]
            chunks.append(chunk)
        return chunks

    def flush(self) -> bytes | None:
        """Return and clear whatever partial audio remains (e.g. on stop)."""
        if not self._buffer:
            return None
        remainder = bytes(self._buffer)
        self._buffer.clear()
        return remainder

    def duration_seconds(self, data: bytes) -> float:
        samples = len(data) / self._bytes_per_sample / self.channels
        return samples / self.sample_rate

    @staticmethod
    def to_numpy(pcm16_bytes: bytes) -> np.ndarray:
        """Decode PCM16LE bytes into a float32 array in [-1, 1], for providers/DSP that want numeric samples."""
        ints = np.frombuffer(pcm16_bytes, dtype="<i2")
        return ints.astype(np.float32) / 32768.0

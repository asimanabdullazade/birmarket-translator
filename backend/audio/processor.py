"""
Server-side audio buffering.

The frontend captures microphone audio with the Web Audio API, resamples it
to a fixed sample rate, and streams it to the backend as raw PCM16LE mono
binary WebSocket frames (see frontend/src/audio/audioCapture.js). This
module accumulates those raw bytes and hands the translation provider
fixed-size chunks, so provider implementations don't need to think about
WebSocket framing at all.
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

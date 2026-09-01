"""
Minimal energy-based voice activity detection.

This is intentionally simple (RMS-over-threshold) so the scaffold has no
extra native dependencies. Swap in a proper model (e.g. webrtcvad, Silero
VAD) here if you need robust silence trimming before sending audio to a
paid transcription API.
"""

from __future__ import annotations

import numpy as np


def is_speech(pcm16_bytes: bytes, threshold: float = 0.01) -> bool:
    """Return True if the chunk's RMS energy suggests speech rather than silence."""
    if not pcm16_bytes:
        return False
    samples = np.frombuffer(pcm16_bytes, dtype="<i2").astype(np.float32) / 32768.0
    if samples.size == 0:
        return False
    rms = float(np.sqrt(np.mean(np.square(samples))))
    return rms >= threshold

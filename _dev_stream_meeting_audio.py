"""
Phase 11 (meeting broadcast mode) dev harness -- NOT an automated test,
a manual tool for exercising the whole ingest -> transcribe -> translate
-> broadcast -> listener pipeline end to end without any real meeting
integration (no Teams bot exists yet, see the Phase 11 plan's "Context").

Streams a 16kHz mono PCM16 WAV file into
/ws/meeting/{meeting_id}/ingest with realistic real-time pacing (~200ms
frames, paced with asyncio.sleep so the backend's VAD segmenter behaves
the way it would against a live speaker, not an instant firehose of the
whole file). Pair this with opening frontend/listener.html?meeting_id=...
in 1-3 browser tabs (one per language: en/az/ru) while this runs, to hear
the whole thing working.

Usage:
    python3 _dev_stream_meeting_audio.py path/to/audio.wav [meeting_id]

The WAV file must be 16-bit PCM, mono, 16000 Hz (matching
config/settings.py's AUDIO_SAMPLE_RATE) -- use ffmpeg to convert if
needed, e.g.:
    ffmpeg -i input.mp3 -ar 16000 -ac 1 -sample_fmt s16 out.wav

Requires the backend running with TRANSLATION_PROVIDER=gemini or
TRANSLATION_PROVIDER=mock (the only two providers that implement
transcribe_final/translate_final -- see backend/translation/base.py and
the provider allowlist in backend/websocket/meeting_handlers.py).
"""

from __future__ import annotations

import asyncio
import json
import sys
import wave

import websockets

FRAME_MS = 200
BACKEND_WS_BASE = "ws://localhost:8000"


async def stream_file(path: str, meeting_id: str) -> None:
    with wave.open(path, "rb") as wf:
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
            raise SystemExit(
                f"{path} must be 16-bit PCM mono -- got {wf.getnchannels()} channel(s), "
                f"{wf.getsampwidth() * 8}-bit. Convert with ffmpeg first (see this file's docstring)."
            )
        sample_rate = wf.getframerate()
        frame_samples = int(sample_rate * FRAME_MS / 1000)
        frames_bytes = frame_samples * 2  # PCM16 = 2 bytes/sample

        url = f"{BACKEND_WS_BASE}/ws/meeting/{meeting_id}/ingest"
        print(f"Connecting to {url} (source sample rate: {sample_rate} Hz)...")

        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({"type": "start", "sample_rate": sample_rate}))
            print("Sent start -- streaming audio in real time (Ctrl+C to stop early)...")

            sent_bytes = 0
            while True:
                chunk = wf.readframes(frame_samples)
                if not chunk:
                    break
                await ws.send(chunk)
                sent_bytes += len(chunk)
                await asyncio.sleep(FRAME_MS / 1000)

            await ws.send(json.dumps({"type": "stop"}))
            print(f"Done -- streamed {sent_bytes / 2 / sample_rate:.1f}s of audio. Sent stop, closing.")

            # Give the backend a moment to finish processing/broadcasting
            # whatever's still in flight before this script exits.
            await asyncio.sleep(2.0)


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)
    path = sys.argv[1]
    meeting_id = sys.argv[2] if len(sys.argv) > 2 else "dev-meeting"
    asyncio.run(stream_file(path, meeting_id))


if __name__ == "__main__":
    main()

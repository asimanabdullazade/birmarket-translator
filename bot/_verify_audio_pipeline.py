"""
Sandbox-verifiable dev harness for the audio_pipeline.py -> ingest_client.py
half of the bot -- the half with zero dependency on Docker or a real
Teams meeting (see the Phase 12 plan's sandbox-constraints section).

What this proves, end to end, using REAL code (not mocks of
audio_pipeline/ingest_client themselves):
  1. A PulseAudio null sink + monitor source can be set up exactly the
     way entrypoint.sh will set one up in the container.
  2. Playing a test tone into that sink (standing in for "Chromium's
     meeting audio output") is capturable by audio_pipeline.py's real
     ffmpeg-based capture_frames() as PCM16LE frames.
  3. Those frames flow through ingest_client.py's real stream_to_ingest()
     into a REAL running backend (backend/main.py, TRANSLATION_PROVIDER=
     mock) at the exact same /ws/meeting/{id}/ingest endpoint the browser
     mic broadcaster (frontend/src/MeetingBroadcast.jsx) and the WAV-file
     harness (_dev_stream_meeting_audio.py) already use.

This intentionally reuses the *actual* bot modules, not fakes of them --
the whole point is to prove the real pipeline code works before ever
touching Playwright/Teams.

Usage (from the bot/ directory, with PulseAudio not yet running):
    # 1. In one terminal: start the backend with the mock provider
    cd .. && TRANSLATION_PROVIDER=mock python -m uvicorn backend.main:app --port 8000

    # 2. In another terminal:
    cd bot && python3 _verify_audio_pipeline.py [path/to/test_tone.wav]

If no WAV path is given, a short synthetic sine-wave tone is generated
on the fly (no test fixture needed to check this in).
"""

from __future__ import annotations

import asyncio
import math
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import BotSettings  # noqa: E402
from logging_utils import configure_logging  # noqa: E402
import audio_pipeline  # noqa: E402
import ingest_client  # noqa: E402

CHECK = "✓"
CROSS = "✗"


def _generate_test_tone(path: str, seconds: float = 4.0, sample_rate: int = 16000, freq_hz: float = 440.0) -> None:
    n_samples = int(seconds * sample_rate)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        frames = bytearray()
        for i in range(n_samples):
            value = int(12000 * math.sin(2 * math.pi * freq_hz * i / sample_rate))
            frames.extend(struct.pack("<h", value))
        wf.writeframes(bytes(frames))


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def setup_pulseaudio(sink_name: str) -> None:
    print(f"Setting up PulseAudio null sink '{sink_name}'...")
    # Start (or confirm) a user-mode PulseAudio daemon. --start is
    # idempotent -- safe to call even if one's already running.
    _run(["pulseaudio", "--start", "--exit-idle-time=-1"], check=False)
    # Load the null sink -- ignore "module already loaded"-type errors so
    # this script is re-runnable.
    _run(
        ["pactl", "load-module", "module-null-sink", f"sink_name={sink_name}",
         "sink_properties=device.description=VerifyMeetingSink"],
        check=False,
    )
    _run(["pactl", "set-default-sink", sink_name], check=False)
    print(f"{CHECK} PulseAudio sink ready (or already was)")


async def play_tone_into_sink(wav_path: str) -> asyncio.subprocess.Process:
    """Start `paplay` playing wav_path into the current default sink, as a background process."""
    proc = await asyncio.create_subprocess_exec(
        "paplay", wav_path,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return proc


async def listen_for_transcript(ws_base: str, meeting_id: str, lang: str, received: list) -> None:
    """
    Connect as a real listener (the same /ws/meeting/{id}/listen?lang=
    endpoint frontend/src/MeetingListener.jsx uses) and record every
    message it gets, so this script can *prove* the utterance was
    actually processed and broadcast -- not just that no exception was
    raised. Runs until cancelled.
    """
    import websockets as _websockets

    url = f"{ws_base}/ws/meeting/{meeting_id}/listen?lang={lang}"
    async with _websockets.connect(url) as ws:
        async for message in ws:
            received.append(message)


async def main() -> None:
    settings = BotSettings(
        meeting_id="verify-audio-pipeline",
        backend_ws_base="ws://localhost:8000",
        pulse_sink_name="verifysink",
    )
    logger = configure_logging(settings.log_level)

    received_messages: list = []
    listener_task = asyncio.create_task(
        listen_for_transcript(settings.backend_ws_base, settings.meeting_id, "en", received_messages)
    )
    await asyncio.sleep(0.5)  # let the listener register in the room before the ingest side starts

    wav_path = sys.argv[1] if len(sys.argv) > 1 else None
    tmp_wav = None
    if wav_path is None:
        tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp_wav.close()
        wav_path = tmp_wav.name
        print(f"No WAV path given -- generating a 4s test tone at {wav_path}")
        _generate_test_tone(wav_path, sample_rate=settings.audio_sample_rate)

    setup_pulseaudio(settings.pulse_sink_name)

    print("Starting ffmpeg capture from the sink's monitor...")
    ffmpeg_proc = await audio_pipeline.start_ffmpeg_capture(settings, logger)

    print(f"Playing test tone into the sink ({wav_path})...")
    play_proc = await play_tone_into_sink(wav_path)

    stop_event = asyncio.Event()

    async def frames_with_timeout():
        frame_count = 0
        max_frames = int(6.0 * 1000 / settings.frame_ms)  # ~6s worth, generous margin over the 4s tone
        async for frame in audio_pipeline.capture_frames(ffmpeg_proc, settings, logger):
            yield frame
            frame_count += 1
            if frame_count >= max_frames:
                break
        stop_event.set()

    print("Streaming captured frames into the real ingest WebSocket "
          f"({settings.backend_ws_base}/ws/meeting/{settings.meeting_id}/ingest)...")
    try:
        await ingest_client.stream_to_ingest(settings, frames_with_timeout(), stop_event, logger)
    except ingest_client.IngestError as exc:
        print(f"{CROSS} Ingest failed: {exc}")
        print("  Is the backend running? e.g.:")
        print("    TRANSLATION_PROVIDER=mock python -m uvicorn backend.main:app --port 8000")
        sys.exit(1)
    finally:
        await audio_pipeline.stop_capture(ffmpeg_proc, logger)
        if play_proc.returncode is None:
            play_proc.terminate()
        if tmp_wav is not None:
            Path(tmp_wav.name).unlink(missing_ok=True)

    # Give the backend a moment to finish broadcasting to the listener
    # before we check what it received.
    await asyncio.sleep(1.0)
    listener_task.cancel()
    try:
        await listener_task
    except asyncio.CancelledError:
        pass

    transcript_messages = [m for m in received_messages if '"type":"transcript"' in m or '"type": "transcript"' in m]
    print(f"Listener received {len(received_messages)} message(s) total, "
          f"{len(transcript_messages)} of them transcripts.")
    for m in received_messages:
        print(f"  <- {m[:200]}")

    if not transcript_messages:
        print(f"{CROSS} No transcript message reached a real listener -- the pipeline connected and sent bytes, "
              "but nothing appears to have been transcribed/broadcast. Check VAD_THRESHOLD, the tone's volume, "
              "and backend logs.")
        sys.exit(1)

    print(f"{CHECK} Audio pipeline verified END TO END: PulseAudio sink -> ffmpeg capture -> real ingest "
          "WebSocket -> backend VAD/mock-transcribe -> broadcast -> a real listener socket actually received it.")


if __name__ == "__main__":
    asyncio.run(main())

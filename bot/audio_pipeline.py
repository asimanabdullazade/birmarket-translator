"""
Owns getting raw meeting audio out of the container: spawns ffmpeg
reading the PulseAudio null sink's monitor source, and exposes it as an
async stream of fixed-size PCM16LE frames.

Frame sizing mirrors _dev_stream_meeting_audio.py exactly
(frame_samples = sample_rate * frame_ms / 1000; frame_bytes = frame_samples
* 2, since PCM16 is 2 bytes/sample) so the ingest side sees the same
shape of frame regardless of whether it came from a WAV file, a browser
mic, or this pipeline.

IMPORTANT divergence from _dev_stream_meeting_audio.py: that script has
to *artificially* pace playback of a static WAV file with
asyncio.sleep(FRAME_MS / 1000), because a file has no inherent tempo.
Here, the source is a live PulseAudio monitor of whatever Chromium is
actually outputting -- it's already real-time-paced by the audio
hardware/timing itself. capture_frames() below just awaits new bytes as
ffmpeg produces them; it must NOT add its own sleep. Copying that sleep
here would silently desync capture from the live meeting (e.g. producing
frames faster or slower than real time), which is a subtle, easy mistake
to make when adapting the WAV-file pattern -- flagged here explicitly so
it isn't reintroduced later.
"""

from __future__ import annotations

import array
import asyncio
import logging
import math
import time
from typing import AsyncIterator

from config import BotSettings


def _rms(pcm16le_bytes: bytes) -> int:
    """
    Root-mean-square level of a PCM16LE buffer, as a rough silence check.
    Deliberately hand-rolled instead of using the stdlib `audioop` module
    -- audioop is deprecated since Python 3.11 and removed outright in
    3.13 (PEP 594), and this bot's exact Python version depends on
    whatever the pinned Playwright base image ships, which may already be
    3.13+. This has no such version dependency.
    """
    if len(pcm16le_bytes) < 2:
        return 0
    samples = array.array("h")  # signed 16-bit
    usable_len = len(pcm16le_bytes) - (len(pcm16le_bytes) % 2)
    samples.frombytes(pcm16le_bytes[:usable_len])
    if not samples:
        return 0
    sum_squares = sum(s * s for s in samples)
    return int(math.sqrt(sum_squares / len(samples)))


def frame_size_bytes(settings: BotSettings) -> int:
    frame_samples = int(settings.audio_sample_rate * settings.frame_ms / 1000)
    return frame_samples * 2  # PCM16LE = 2 bytes/sample


async def start_ffmpeg_capture(settings: BotSettings, logger: logging.Logger) -> asyncio.subprocess.Process:
    """
    Spawn ffmpeg reading the PulseAudio null sink's monitor source as raw
    PCM16LE at the configured sample rate/channel count, writing to
    stdout. Requires PulseAudio + the null sink to already be running
    (entrypoint.sh does this before bot.py starts) and PULSE_SERVER/
    PULSE_SINK-equivalent env plumbing to be in place so ffmpeg's `-f
    pulse` input actually reaches the right server -- see bot/README.md's
    troubleshooting section if this connects but yields only silence.
    """
    monitor_source = f"{settings.pulse_sink_name}.monitor"
    cmd = [
        "ffmpeg",
        "-loglevel", "warning",
        "-f", "pulse",
        "-i", monitor_source,
        "-ar", str(settings.audio_sample_rate),
        "-ac", "1",
        "-f", "s16le",
        "-",
    ]
    logger.info("starting ffmpeg capture: %s", " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    return proc


async def capture_frames(
    proc: asyncio.subprocess.Process,
    settings: BotSettings,
    logger: logging.Logger,
) -> AsyncIterator[bytes]:
    """
    Yield fixed-size PCM16LE frames read from ffmpeg's stdout, in real
    time, as they arrive -- no artificial pacing (see module docstring).
    Logs RMS/silence stats on the first several frames so "the pipe is
    connected but silent" -- the single biggest unverified risk in this
    whole bot, per the Phase 12 plan -- is diagnosable immediately rather
    than discovered downstream in the backend's VAD segmenter.
    """
    assert proc.stdout is not None
    chunk_bytes = frame_size_bytes(settings)

    # Rolling RMS statistics, logged periodically for the WHOLE session
    # rather than for the first N frames.
    #
    # The original version logged the first 10 frames and then went quiet
    # forever. Those 10 frames are all captured within ~4 seconds of
    # joining -- long before anyone in the meeting has said anything --
    # so they were guaranteed to read rms=0 whether the audio path worked
    # or not. That made "the pipe is silent" and "nobody has spoken yet"
    # indistinguishable, which is exactly the question this logging
    # exists to answer.
    stats_interval_s = 2.0
    silence_warn_after_s = 30.0
    audible_rms = 50  # above room-tone/dither, below any real speech

    total_frames = 0
    window_frames = 0
    window_peak = 0
    window_sum = 0
    started_at = time.monotonic()
    last_stats_at = started_at
    ever_audible = False
    warned_silent = False

    while True:
        data = await _read_exact_or_less(proc.stdout, chunk_bytes)
        if not data:
            break

        level = _rms(data)
        total_frames += 1
        window_frames += 1
        window_sum += level
        window_peak = max(window_peak, level)

        if not ever_audible and level >= audible_rms:
            ever_audible = True
            logger.info(
                "AUDIO DETECTED after %.1fs (rms=%d) -- the PulseAudio null sink is working",
                time.monotonic() - started_at,
                level,
            )

        now = time.monotonic()
        if now - last_stats_at >= stats_interval_s:
            logger.info(
                "audio stats: %d frames in %.1fs, peak rms=%d, mean rms=%d (%d frames total)",
                window_frames,
                now - last_stats_at,
                window_peak,
                window_sum // max(window_frames, 1),
                total_frames,
            )

            if (
                not ever_audible
                and not warned_silent
                and now - started_at >= silence_warn_after_s
            ):
                warned_silent = True
                logger.error(
                    "%.0fs of capture with peak rms=0 throughout -- Chromium's audio is not reaching "
                    "the null sink. Two things to check, in order: (1) confirm --mute-audio is absent "
                    "from Chromium's argv (it is one of Playwright's DEFAULT args and must be removed "
                    "via ignore_default_args, see browser_join.py's launch_browser); (2) confirm a "
                    "sink-input exists on the sink -- `pactl list sink-inputs` inside the container. "
                    "See bot/README.md's troubleshooting section.",
                    now - started_at,
                )

            last_stats_at = now
            window_frames = 0
            window_peak = 0
            window_sum = 0

        yield data

    logger.info(
        "ffmpeg capture stream ended after %d frames (%.1fs); audio was %sdetected during this session",
        total_frames,
        time.monotonic() - started_at,
        "" if ever_audible else "NEVER ",
    )


async def _read_exact_or_less(stream: asyncio.StreamReader, n: int) -> bytes:
    """
    Like stream.readexactly(n), but returns whatever was read (possibly
    empty) on EOF instead of raising IncompleteReadError -- a clean EOF
    (ffmpeg exited) should end the frame stream, not crash it.
    """
    buf = bytearray()
    while len(buf) < n:
        chunk = await stream.read(n - len(buf))
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


async def stop_capture(proc: asyncio.subprocess.Process, logger: logging.Logger, timeout_s: float = 5.0) -> None:
    """Terminate the ffmpeg subprocess cleanly, killing it if it won't exit."""
    if proc.returncode is not None:
        return
    logger.info("stopping ffmpeg capture (pid=%s)", proc.pid)
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout_s)
    except asyncio.TimeoutError:
        logger.warning("ffmpeg did not exit within %.1fs, killing it", timeout_s)
        proc.kill()
        await proc.wait()

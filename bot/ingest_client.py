"""
Owns getting captured frames onto the meeting's ingest WebSocket -- a
near-verbatim adaptation of _dev_stream_meeting_audio.py's stream_file(),
just fed by a live AsyncIterator[bytes] (audio_pipeline.capture_frames)
instead of a WAV file's readframes() loop, and with no artificial pacing
sleep (see audio_pipeline.py's module docstring for why).

Talks the exact same wire protocol backend/websocket/meeting_handlers.py
already expects -- unchanged: {"type":"start","sample_rate":N} once, raw
PCM16LE binary frames, {"type":"stop"} once. No backend changes needed
for this bot to work at all; that's the whole point of Phase 11's
ingest contract.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, Optional

import websockets

from config import BotSettings


class IngestError(Exception):
    """Raised when the ingest WebSocket connection fails or drops."""


async def stream_to_ingest(
    settings: BotSettings,
    frame_source: AsyncIterator[bytes],
    stop_event: asyncio.Event,
    logger: logging.Logger,
) -> None:
    """
    Connect to the meeting's ingest WebSocket, send `start`, forward every
    frame from `frame_source` until `stop_event` is set or the source is
    exhausted, then send `stop` and give the backend a moment to finish
    processing in-flight audio before returning.

    v1 does NOT reconnect a dropped ingest socket mid-meeting -- a drop
    here is treated as fatal for this run (raises IngestError) and logged
    clearly; recovering requires restarting the bot container. This is a
    stated v1 limitation (see bot/README.md), not an oversight.
    """
    url = f"{settings.backend_ws_base}/ws/meeting/{settings.meeting_id}/ingest"
    logger.info("connecting to ingest endpoint: %s", url)

    try:
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({"type": "start", "sample_rate": settings.audio_sample_rate}))
            logger.info("sent start -- streaming captured meeting audio")

            sent_bytes = 0
            frame_iter = frame_source.__aiter__()

            while not stop_event.is_set():
                next_frame_task = asyncio.ensure_future(frame_iter.__anext__())
                stop_wait_task = asyncio.ensure_future(stop_event.wait())
                done, pending = await asyncio.wait(
                    {next_frame_task, stop_wait_task}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()

                if next_frame_task in done:
                    try:
                        chunk = next_frame_task.result()
                    except StopAsyncIteration:
                        logger.info("audio frame source exhausted")
                        break
                    await ws.send(chunk)
                    sent_bytes += len(chunk)
                else:
                    # stop_event fired first -- discard whatever the
                    # in-flight frame future eventually resolves to.
                    next_frame_task.cancel()
                    break

            await ws.send(json.dumps({"type": "stop"}))
            duration_s = sent_bytes / 2 / settings.audio_sample_rate
            logger.info("sent stop -- streamed %.1fs of audio (%d bytes)", duration_s, sent_bytes)

            # Give the backend a moment to finish processing/broadcasting
            # whatever's still in flight before this task returns, same
            # as _dev_stream_meeting_audio.py's trailing sleep.
            await asyncio.sleep(2.0)

    except (websockets.exceptions.WebSocketException, OSError) as exc:
        raise IngestError(f"ingest WebSocket connection failed or dropped: {exc}") from exc

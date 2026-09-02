"""
Per-connection protocol handler.

This is the piece that ties everything together: it reads control/audio
frames off one WebSocket, drives an `AudioBuffer` (backend/audio) and a
`TranslationProvider` (backend/translation), and writes status/transcript/
translation frames back, per the protocol defined in backend/models/schemas.py.

Message flow
------------
1. Client connects; server sends `status: connected`.
2. Client sends a `start` message with source_lang/target_lang.
   Server opens a provider session, starts a background task to process
   buffered audio, and sends `status: listening`.
3. Client streams binary PCM16LE audio frames. Each frame is buffered;
   once enough audio has accumulated, the chunk is handed off to a queue
   -- *not* processed inline -- so reading the next incoming frame never
   waits on a slow provider call (see "Why a queue" below). The background
   task drains the queue, calling the provider and relaying any
   transcript/translation events, with `status: translating`/`listening`
   toggled around that work.
4. Client sends `stop`: server flushes remaining audio, lets the
   background task finish processing whatever's already queued (the
   socket is still open, so it's worth sending final results), closes the
   provider session, sends any final events, then `status: connected`.
5. Client disconnects unexpectedly: server *cancels* the background task
   instead of draining it -- see "Drain vs. cancel" below.

Errors at any step are reported as an `error` message rather than closing
the socket, so the client can show it and try again without reconnecting.

Why a queue
-----------
Earlier versions called `await provider.process_audio_chunk(chunk)` right
in the same loop that reads frames off the socket. That's fine with the
"mock" provider (near-instant), but with a real provider (a Gemini API
round-trip, or CPU-bound local Whisper/NLLB inference) that await can take
seconds -- during which this connection's task wasn't calling
`websocket.receive()` at all. Audio kept arriving from the browser the
whole time, but sat unread in the OS socket buffer, which then looks
exactly like a capture "gap" once it's finally read, even though the
browser never stopped sending. Moving provider calls onto a background
task fed by a queue keeps the read loop free to keep up with the incoming
stream regardless of how slow any single translation call is.

Drain vs. cancel
----------------
That queue can build up a backlog if the provider is slower than the
audio arrives (exactly what a Gemini/local-model round-trip can cause).
On a clean `stop`, the socket is still open, so it's worth letting the
background task finish that backlog and send final results -- that's
`_drain_consumer()`. But if the client disconnects instead, the socket is
already dead: draining would mean the task keeps calling the (possibly
slow, possibly paid) provider and then trying to send on a closed socket
for every backlogged chunk, which is both wasted work and -- as seen in
practice -- floods the log with "Cannot call send once a close message
has been sent" errors once the first send fails. So a real disconnect
instead calls `_cancel_consumer()`, which cancels the task immediately and
discards whatever was still queued. Every send anywhere in this module
also goes through `_safe_send`, which swallows the (expected, benign) case
of the client already being gone rather than raising.

Mic-capture diagnostics
------------------------
Two lightweight checks run on every incoming binary frame, independent of
whatever TRANSLATION_PROVIDER is selected -- useful for verifying the raw
capture/streaming pipeline (see the "Testing the microphone pipeline"
section in the README) before worrying about translation quality at all:

- Gap detection: warns if too long passes between two consecutive frames
  actually being read off the socket (see "Why a queue" above for why that
  now reflects real capture/network gaps rather than provider latency).
- Duplicate detection: warns if a frame is byte-for-byte identical to the
  one immediately before it *and* contains speech. (Silence legitimately
  repeats byte-for-byte -- two all-zero chunks are not a bug -- so the
  check ignores silent frames via the same VAD used by translation
  providers to skip silence.)

Additionally, setting DEBUG_AUDIO_DUMP_DIR (see config/settings.py) records
each session's raw incoming audio to a .wav file so you can listen back to
exactly what the backend received.
"""

from __future__ import annotations

import asyncio
import logging
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from backend.audio.processor import AudioBuffer
from backend.audio.vad import is_speech
from backend.models.schemas import (
    ErrorMessage,
    StartMessage,
    StatusMessage,
    StopMessage,
    TranscriptMessage,
    TranslationMessage,
    parse_client_message,
)
from backend.translation.base import EventKind, TranslationEvent
from backend.translation.factory import get_provider
from config.languages import is_supported
from config.settings import Settings

logger = logging.getLogger(__name__)

# If more than this many seconds pass between two consecutive binary audio
# frames actually being read off the socket, log a warning. Frames normally
# arrive every ~100-300ms (see AUDIO_SEND_CHUNK_MS in the frontend), so
# anything over a second is a real anomaly.
GAP_WARNING_SECONDS = 1.0

# Sentinel put on the processing queue to tell the consumer task to stop.
_STOP = object()


async def _safe_send(websocket: WebSocket, payload: str) -> bool:
    """Send a text frame, swallowing errors caused by the client already
    being gone -- a normal race (it can disconnect at any moment while we're
    mid-send from the background consumer task), not a bug. Returns False if
    the send failed because the socket is no longer usable, so callers can
    stop bothering to send more."""
    try:
        await websocket.send_text(payload)
        return True
    except (RuntimeError, WebSocketDisconnect) as exc:
        logger.debug("Dropped a message because the client is gone: %s", exc)
        return False


def _open_debug_dump(settings: Settings, sample_rate: int) -> Optional[wave.Wave_write]:
    if not settings.debug_audio_dump_dir:
        return None
    directory = Path(settings.debug_audio_dump_dir)
    directory.mkdir(parents=True, exist_ok=True)
    filename = directory / f"session_{datetime.now():%Y%m%d_%H%M%S}.wav"
    wf = wave.open(str(filename), "wb")
    wf.setnchannels(settings.audio_channels)
    wf.setsampwidth(2)  # PCM16
    wf.setframerate(sample_rate)
    logger.info("Recording raw incoming audio to %s", filename)
    return wf


async def _send_events(websocket: WebSocket, events: list[TranslationEvent], target_lang: str, source_lang: str) -> None:
    for event in events:
        if event.kind == EventKind.TRANSCRIPT:
            payload = TranscriptMessage(text=event.text, is_final=event.is_final).model_dump_json()
        else:
            payload = TranslationMessage(
                text=event.text,
                is_final=event.is_final,
                source_lang=source_lang,
                target_lang=target_lang,
            ).model_dump_json()
        if not await _safe_send(websocket, payload):
            return  # client is gone -- no point sending the rest


async def handle_connection(websocket: WebSocket, settings: Settings) -> None:
    await websocket.send_text(StatusMessage(status="connected").model_dump_json())

    audio_buffer: Optional[AudioBuffer] = None
    provider = None
    source_lang = ""
    target_lang = ""
    session_active = False

    debug_dump: Optional[wave.Wave_write] = None
    last_frame_at: Optional[float] = None
    last_frame_bytes: Optional[bytes] = None

    # Chunks ready for translation are handed off here immediately; a
    # background task drains them, so a slow provider call never blocks
    # reading the next audio frame off the socket. See "Why a queue" above.
    queue: "asyncio.Queue" = asyncio.Queue()
    consumer_task: Optional[asyncio.Task] = None

    async def consume() -> None:
        while True:
            item = await queue.get()
            if item is _STOP:
                return
            try:
                if not await _safe_send(websocket, StatusMessage(status="translating").model_dump_json()):
                    continue
                events = await provider.process_audio_chunk(item)
                await _send_events(websocket, events, target_lang, source_lang)
                if queue.empty():
                    await _safe_send(websocket, StatusMessage(status="listening").model_dump_json())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error while processing a buffered audio chunk")

    async def drain_consumer() -> None:
        """Clean `stop`: socket is still open, so finish the backlog and
        let final results actually reach the client."""
        nonlocal consumer_task
        if consumer_task is None:
            return
        await queue.put(_STOP)
        try:
            await consumer_task
        except Exception:  # noqa: BLE001
            logger.exception("Consumer task raised while draining")
        consumer_task = None

    async def cancel_consumer() -> None:
        """Disconnect/error: socket is dead, so stop immediately instead of
        working through a backlog nobody can receive the results of."""
        nonlocal consumer_task
        if consumer_task is None:
            return
        consumer_task.cancel()
        try:
            await consumer_task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            logger.exception("Consumer task raised while cancelling")
        consumer_task = None

    try:
        while True:
            message = await websocket.receive()

            if message.get("type") == "websocket.disconnect":
                break

            if "text" in message and message["text"] is not None:
                try:
                    parsed = parse_client_message(message["text"])
                except ValidationError as exc:
                    await _safe_send(websocket, ErrorMessage(message=f"Invalid message: {exc}").model_dump_json())
                    continue

                if isinstance(parsed, StartMessage):
                    if not is_supported(parsed.source_lang) or not is_supported(parsed.target_lang):
                        await _safe_send(
                            websocket, ErrorMessage(message="Unsupported source or target language").model_dump_json()
                        )
                        continue

                    source_lang, target_lang = parsed.source_lang, parsed.target_lang
                    audio_buffer = AudioBuffer(
                        sample_rate=parsed.sample_rate,
                        channels=settings.audio_channels,
                        chunk_seconds=settings.audio_chunk_seconds,
                    )
                    provider = get_provider(settings)
                    await provider.start_session(source_lang, target_lang)
                    session_active = True
                    last_frame_at = None
                    last_frame_bytes = None
                    debug_dump = _open_debug_dump(settings, parsed.sample_rate)
                    consumer_task = asyncio.create_task(consume())
                    await _safe_send(websocket, StatusMessage(status="listening").model_dump_json())

                elif isinstance(parsed, StopMessage):
                    if session_active and audio_buffer is not None and provider is not None:
                        remainder = audio_buffer.flush()
                        if remainder:
                            if debug_dump is not None:
                                debug_dump.writeframes(remainder)
                            await queue.put(remainder)
                        await drain_consumer()
                        final_events = await provider.close_session()
                        await _send_events(websocket, final_events, target_lang, source_lang)
                    if debug_dump is not None:
                        debug_dump.close()
                        debug_dump = None
                    session_active = False
                    await _safe_send(websocket, StatusMessage(status="connected").model_dump_json())

            elif "bytes" in message and message["bytes"] is not None:
                if not session_active or audio_buffer is None or provider is None:
                    await _safe_send(
                        websocket, ErrorMessage(message="Received audio before a start message").model_dump_json()
                    )
                    continue

                data = message["bytes"]

                now = time.monotonic()
                if last_frame_at is not None:
                    gap = now - last_frame_at
                    if gap > GAP_WARNING_SECONDS:
                        logger.warning("Possible audio gap: %.2fs since the previous frame", gap)
                last_frame_at = now

                if last_frame_bytes is not None and data == last_frame_bytes and is_speech(data):
                    logger.warning(
                        "Received an exact duplicate audio frame (%d bytes) -- possible retransmission bug",
                        len(data),
                    )
                last_frame_bytes = data

                if debug_dump is not None:
                    debug_dump.writeframes(data)

                audio_buffer.add(data)
                for chunk in audio_buffer.pop_ready_chunks():
                    await queue.put(chunk)

    except WebSocketDisconnect:
        logger.info("Client disconnected")
    except Exception as exc:  # noqa: BLE001 - report to client instead of a bare 500/close
        logger.exception("Unhandled error in WebSocket session")
        await _safe_send(websocket, ErrorMessage(message=str(exc)).model_dump_json())
        await _safe_send(websocket, StatusMessage(status="error", detail=str(exc)).model_dump_json())
    finally:
        await cancel_consumer()
        if debug_dump is not None:
            debug_dump.close()
        if session_active and provider is not None:
            try:
                await provider.close_session()
            except Exception:  # noqa: BLE001
                pass

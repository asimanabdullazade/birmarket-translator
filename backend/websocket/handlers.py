"""
Per-connection protocol handler.

This is the piece that ties everything together: it reads control/audio
frames off one WebSocket, drives a `SpeechSegmenter` (backend/audio) and a
`TranslationProvider` (backend/translation), and writes status/transcript/
translation frames back, per the protocol defined in backend/models/schemas.py.

Message flow
------------
1. Client connects; server sends `status: connected`.
2. Client sends a `start` message with source_lang/target_lang.
   Server opens a provider session, starts a background task to process
   completed phrases, and sends `status: listening`.
3. Client streams binary PCM16LE audio frames. Each frame is fed to a
   `SpeechSegmenter` (backend/audio/segmenter.py), which:
     - fires SPEECH_START when a phrase begins (captured as this phrase's
       `started_at` timestamp, shared by every message about it),
     - fires PARTIAL_UPDATE periodically while it continues -- queued as a
       ("partial", audio, started_at) item, transcribed via the cheaper
       `provider.transcribe_partial()` and sent as `is_final=False`. A
       partial identical to the last one sent for this phrase is dropped
       rather than resent (see "Prevent duplicated phrases" in Step 4).
     - fires UTTERANCE_READY once the phrase ends -- queued as a
       ("final", audio, started_at) item, run through the full
       `provider.process_audio_chunk()` (transcript *and* translation),
       and sent as `is_final=True`. Only this final version is meant to be
       kept/stored by the client -- partials are live feedback only.
   Handoff to either path goes through a queue, not a direct call -- see
   "Why a queue" below. The background task drains the queue in order
   (guaranteeing partials for a phrase are always sent before its final),
   with `status: translating`/`listening` toggled around *final* work only
   -- partials are cheap enough not to warrant a status flicker.
4. Client sends `stop`: server flushes whatever phrase was still in
   progress (as a final), lets the background task finish processing
   whatever's already queued (the socket is still open, so it's worth
   sending final results), closes the provider session, sends any final
   events, then `status: connected`.
5. Client disconnects unexpectedly: server *cancels* the background task
   instead of draining it -- see "Drain vs. cancel" below.

Errors at any step are reported as an `error` message rather than closing
the socket, so the client can show it and try again without reconnecting.

Why a queue
-----------
Calling `await provider.process_audio_chunk(...)` right in the loop that
reads frames off the socket would mean a slow provider call (a Gemini API
round-trip, or CPU-bound local Whisper/NLLB inference) blocks reading the
*next* incoming frame -- audio would keep arriving from the browser but sit
unread in the OS socket buffer, which then looks exactly like a capture
gap even though the browser never stopped sending. Moving provider calls
onto a background task fed by a queue keeps the read loop free to keep up
with the incoming stream (and keep segmenting it correctly) regardless of
how slow any single translation call is.

Drain vs. cancel
----------------
That queue can build up a backlog if the provider is slower than phrases
arrive. On a clean `stop`, the socket is still open, so it's worth letting
the background task finish that backlog and send final results -- that's
`drain_consumer()`. But if the client disconnects instead, the socket is
already dead: draining would mean the task keeps calling the provider and
then trying to send on a closed socket for every backlogged item, which is
wasted work and floods the log with send-on-closed-socket errors. A real
disconnect instead calls `cancel_consumer()`, which cancels the task
immediately and discards whatever was still queued. Every send anywhere in
this module goes through `_safe_send`, which swallows the (expected,
benign) case of the client already being gone rather than raising.

Diagnostics
-----------
Independent of VAD, two lightweight transport-level checks run on every
incoming binary frame (useful for verifying the raw capture/streaming
pipeline -- see "Testing the microphone pipeline" in the README):

- Gap detection: warns if too long passes between two consecutive frames
  actually being read off the socket.
- Duplicate detection: warns if a frame is byte-for-byte identical to the
  one immediately before it *and* contains speech (silence legitimately
  repeats byte-for-byte, so silent frames are exempt).

And independent of that, VAD phrase boundaries are logged at INFO level
(speech started / an utterance of N seconds was queued), and setting
DEBUG_AUDIO_DUMP_DIR (see config/settings.py) records both the whole
session's raw audio (raw.wav) and each individual detected *final* phrase
(utterance_001.wav, utterance_002.wav, ...) to .wav files -- the latter is
the direct way to check VAD is placing phrase boundaries correctly (not
clipping the first word, not fragmenting a sentence on a short pause).
"""

from __future__ import annotations

import asyncio
import logging
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from backend.audio.segmenter import SegmenterEventKind, SpeechSegmenter
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _open_debug_session_dir(settings: Settings) -> Optional[Path]:
    if not settings.debug_audio_dump_dir:
        return None
    session_dir = Path(settings.debug_audio_dump_dir) / f"session_{datetime.now():%Y%m%d_%H%M%S}"
    session_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Recording debug audio for this session to %s", session_dir)
    return session_dir


def _open_wav(path: Path, sample_rate: int, channels: int) -> wave.Wave_write:
    wf = wave.open(str(path), "wb")
    wf.setnchannels(channels)
    wf.setsampwidth(2)  # PCM16
    wf.setframerate(sample_rate)
    return wf


def _dump_utterance(session_dir: Path, sample_rate: int, channels: int, index: int, audio: bytes) -> None:
    wf = _open_wav(session_dir / f"utterance_{index:03d}.wav", sample_rate, channels)
    wf.writeframes(audio)
    wf.close()


async def _send_events(
    websocket: WebSocket, events: list[TranslationEvent], target_lang: str, source_lang: str, timestamp: str
) -> None:
    for event in events:
        if event.kind == EventKind.TRANSCRIPT:
            payload = TranscriptMessage(
                text=event.text,
                is_final=event.is_final,
                timestamp=timestamp,
                detected_language=event.detected_language,
            ).model_dump_json()
        else:
            payload = TranslationMessage(
                text=event.text,
                is_final=event.is_final,
                source_lang=source_lang,
                target_lang=target_lang,
                timestamp=timestamp,
            ).model_dump_json()
        if not await _safe_send(websocket, payload):
            return  # client is gone -- no point sending the rest


async def handle_connection(websocket: WebSocket, settings: Settings) -> None:
    await websocket.send_text(StatusMessage(status="connected").model_dump_json())

    segmenter: Optional[SpeechSegmenter] = None
    provider = None
    source_lang = ""
    target_lang = ""
    session_active = False

    debug_session_dir: Optional[Path] = None
    raw_dump: Optional[wave.Wave_write] = None
    utterance_index = 0
    last_frame_at: Optional[float] = None
    last_frame_bytes: Optional[bytes] = None

    # Timestamp of when the *current* phrase started (set on SPEECH_START,
    # shared by every partial and the eventual final for that phrase).
    phrase_started_at: str = _now_iso()
    # Last partial transcript text actually sent for the current phrase, so
    # an unchanged re-transcription of the same growing audio isn't resent
    # (see "Prevent duplicated phrases" in Step 4).
    last_partial_text: Optional[str] = None

    # Completed phrases (and interim updates) are handed off here
    # immediately; a background task drains them in order, so a slow
    # provider call never blocks reading the next audio frame (or
    # segmenting it) off the socket. See "Why a queue" above. Items are
    # ("final" | "partial", audio_bytes, started_at_timestamp) tuples.
    queue: "asyncio.Queue" = asyncio.Queue()
    consumer_task: Optional[asyncio.Task] = None

    async def consume() -> None:
        nonlocal last_partial_text
        while True:
            item = await queue.get()
            if item is _STOP:
                return
            kind, audio, started_at = item
            try:
                if kind == "partial":
                    text = await provider.transcribe_partial(audio)
                    if text and text != last_partial_text:
                        last_partial_text = text
                        await _safe_send(
                            websocket,
                            TranscriptMessage(text=text, is_final=False, timestamp=started_at).model_dump_json(),
                        )
                    continue

                # kind == "final"
                if not await _safe_send(websocket, StatusMessage(status="translating").model_dump_json()):
                    continue
                events = await provider.process_audio_chunk(audio)
                await _send_events(websocket, events, target_lang, source_lang, started_at)
                last_partial_text = None
                if queue.empty():
                    await _safe_send(websocket, StatusMessage(status="listening").model_dump_json())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error while processing a buffered utterance")

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

    def enqueue_final(audio: bytes, sample_rate: int) -> None:
        nonlocal utterance_index
        utterance_index += 1
        duration_s = len(audio) / 2 / settings.audio_channels / sample_rate
        logger.info("VAD: speech ended -- utterance #%d, %.2fs queued for translation", utterance_index, duration_s)
        if debug_session_dir is not None:
            _dump_utterance(debug_session_dir, sample_rate, settings.audio_channels, utterance_index, audio)
        queue.put_nowait(("final", audio, phrase_started_at))

    def enqueue_partial(audio: bytes) -> None:
        logger.debug("VAD: partial update -- %d bytes queued for interim transcription", len(audio))
        queue.put_nowait(("partial", audio, phrase_started_at))

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
                    segmenter = SpeechSegmenter(
                        sample_rate=parsed.sample_rate,
                        channels=settings.audio_channels,
                        pre_speech_ms=settings.vad_pre_speech_ms,
                        end_silence_ms=settings.vad_end_silence_ms,
                        vad_threshold=settings.vad_threshold,
                        partial_interval_ms=settings.vad_partial_interval_ms,
                    )
                    provider = get_provider(settings)
                    await provider.start_session(source_lang, target_lang)
                    session_active = True
                    last_frame_at = None
                    last_frame_bytes = None
                    last_partial_text = None
                    utterance_index = 0
                    debug_session_dir = _open_debug_session_dir(settings)
                    raw_dump = (
                        _open_wav(debug_session_dir / "raw.wav", parsed.sample_rate, settings.audio_channels)
                        if debug_session_dir is not None
                        else None
                    )
                    consumer_task = asyncio.create_task(consume())
                    await _safe_send(websocket, StatusMessage(status="listening").model_dump_json())

                elif isinstance(parsed, StopMessage):
                    if session_active and segmenter is not None and provider is not None:
                        remainder = segmenter.flush()
                        if remainder:
                            enqueue_final(remainder, segmenter.sample_rate)
                        await drain_consumer()
                        final_events = await provider.close_session()
                        await _send_events(websocket, final_events, target_lang, source_lang, phrase_started_at)
                    if raw_dump is not None:
                        raw_dump.close()
                        raw_dump = None
                    session_active = False
                    await _safe_send(websocket, StatusMessage(status="connected").model_dump_json())

            elif "bytes" in message and message["bytes"] is not None:
                if not session_active or segmenter is None or provider is None:
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

                if raw_dump is not None:
                    raw_dump.writeframes(data)

                for event in segmenter.push(data):
                    if event.kind == SegmenterEventKind.SPEECH_START:
                        phrase_started_at = _now_iso()
                        last_partial_text = None
                        logger.info("VAD: speech started")
                    elif event.kind == SegmenterEventKind.PARTIAL_UPDATE:
                        enqueue_partial(event.audio)
                    elif event.kind == SegmenterEventKind.UTTERANCE_READY:
                        enqueue_final(event.audio, segmenter.sample_rate)

    except WebSocketDisconnect:
        logger.info("Client disconnected")
    except Exception as exc:  # noqa: BLE001 - report to client instead of a bare 500/close
        logger.exception("Unhandled error in WebSocket session")
        await _safe_send(websocket, ErrorMessage(message=str(exc)).model_dump_json())
        await _safe_send(websocket, StatusMessage(status="error", detail=str(exc)).model_dump_json())
    finally:
        await cancel_consumer()
        if raw_dump is not None:
            raw_dump.close()
        if session_active and provider is not None:
            try:
                await provider.close_session()
            except Exception:  # noqa: BLE001
                pass

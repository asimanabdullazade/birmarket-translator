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
   Server opens a provider session and sends `status: listening`.
3. Client streams binary PCM16LE audio frames. Each frame is buffered;
   once enough audio has accumulated the buffer's chunk is handed to the
   provider, which may emit transcript/translation events. Server sends
   `status: translating` around that call and relays any events.
4. Client sends `stop` (or disconnects). Server flushes remaining audio,
   closes the provider session, sends any final events, then
   `status: connected` (or the socket closes).

Errors at any step are reported as an `error` message rather than closing
the socket, so the client can show it and try again without reconnecting.
"""

from __future__ import annotations

import logging

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from backend.audio.processor import AudioBuffer
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


async def _send_events(websocket: WebSocket, events: list[TranslationEvent], target_lang: str, source_lang: str) -> None:
    for event in events:
        if event.kind == EventKind.TRANSCRIPT:
            await websocket.send_text(
                TranscriptMessage(text=event.text, is_final=event.is_final).model_dump_json()
            )
        else:
            await websocket.send_text(
                TranslationMessage(
                    text=event.text,
                    is_final=event.is_final,
                    source_lang=source_lang,
                    target_lang=target_lang,
                ).model_dump_json()
            )


async def handle_connection(websocket: WebSocket, settings: Settings) -> None:
    await websocket.send_text(StatusMessage(status="connected").model_dump_json())

    audio_buffer: AudioBuffer | None = None
    provider = None
    source_lang = ""
    target_lang = ""
    session_active = False

    try:
        while True:
            message = await websocket.receive()

            if message.get("type") == "websocket.disconnect":
                break

            if "text" in message and message["text"] is not None:
                try:
                    parsed = parse_client_message(message["text"])
                except ValidationError as exc:
                    await websocket.send_text(
                        ErrorMessage(message=f"Invalid message: {exc}").model_dump_json()
                    )
                    continue

                if isinstance(parsed, StartMessage):
                    if not is_supported(parsed.source_lang) or not is_supported(parsed.target_lang):
                        await websocket.send_text(
                            ErrorMessage(message="Unsupported source or target language").model_dump_json()
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
                    await websocket.send_text(StatusMessage(status="listening").model_dump_json())

                elif isinstance(parsed, StopMessage):
                    if session_active and audio_buffer is not None and provider is not None:
                        remainder = audio_buffer.flush()
                        events: list[TranslationEvent] = []
                        if remainder:
                            events += await provider.process_audio_chunk(remainder)
                        events += await provider.close_session()
                        await _send_events(websocket, events, target_lang, source_lang)
                    session_active = False
                    await websocket.send_text(StatusMessage(status="connected").model_dump_json())

            elif "bytes" in message and message["bytes"] is not None:
                if not session_active or audio_buffer is None or provider is None:
                    await websocket.send_text(
                        ErrorMessage(message="Received audio before a start message").model_dump_json()
                    )
                    continue

                audio_buffer.add(message["bytes"])
                ready_chunks = audio_buffer.pop_ready_chunks()
                if not ready_chunks:
                    continue

                await websocket.send_text(StatusMessage(status="translating").model_dump_json())
                for chunk in ready_chunks:
                    events = await provider.process_audio_chunk(chunk)
                    await _send_events(websocket, events, target_lang, source_lang)

                if session_active:
                    await websocket.send_text(StatusMessage(status="listening").model_dump_json())

    except WebSocketDisconnect:
        logger.info("Client disconnected")
    except Exception as exc:  # noqa: BLE001 - report to client instead of a bare 500/close
        logger.exception("Unhandled error in WebSocket session")
        try:
            await websocket.send_text(ErrorMessage(message=str(exc)).model_dump_json())
            await websocket.send_text(StatusMessage(status="error", detail=str(exc)).model_dump_json())
        except Exception:  # noqa: BLE001 - socket may already be closed
            pass
    finally:
        if session_active and provider is not None:
            try:
                await provider.close_session()
            except Exception:  # noqa: BLE001
                pass

"""
Phase 11 (meeting broadcast mode): per-meeting ingest/listener handlers.

Two new WebSocket roles, wired to routes in backend/main.py:

- **Ingest** (`handle_meeting_ingest`, one per meeting): the single source
  of audio for a meeting -- a real bot (not built this phase, see the
  Phase 11 plan) or, for now, `_dev_stream_meeting_audio.py`. Deliberately
  NOT built by reusing `handlers.py`'s `handle_connection` closures --
  that function is tightly bound to one socket driving one fixed
  (source_lang, target_lang) pair for one listener; meeting mode needs to
  auto-detect the speaker's language per utterance and fan that single
  utterance out into up to two *other* languages. Reuses the same
  `SpeechSegmenter`/queue-consumer *shape* as `handlers.py`, and the same
  wire schemas listeners already understand (TranscriptMessage/
  TranslationMessage/AudioMessage) -- but every queued item is a complete
  utterance ("final" only; v1 sends no interim captions in meeting mode,
  see the Phase 11 plan).

- **Listener** (`handle_meeting_listener`, many per meeting): a companion
  page (frontend/src/MeetingListener.jsx) joins one `(meeting_id, lang)`
  room via `MeetingRegistry.add_listener` and then does nothing but wait
  to be disconnected -- `handle_meeting_ingest`'s pipeline is the *only*
  writer to any listener socket, via `registry.broadcast`. This one-
  writer-many-readers split is what keeps provider-call volume flat
  regardless of listener count: one utterance is transcribed exactly
  once and translated into at most `len(meeting_languages) - 1` targets,
  no matter whether a room has 0, 1, or 200 listeners in it.

Per-utterance pipeline (`_process_utterance`)
----------------------------------------------
1. `provider.transcribe_final(audio)` -- exactly once. Empty/unsupported
   result -> drop the utterance.
2. If the detected language isn't one of `settings.meeting_languages`,
   drop it (v1 never guesses).
3. Broadcast the transcript, unmodified, to the `(meeting_id,
   detected_lang)` room -- same-language listeners hear the live meeting
   audio directly and just want captions; zero extra provider calls.
4. For every OTHER language in `settings.meeting_languages`:
   `provider.translate_final(...)`, then broadcast a TranslationMessage
   and stream AudioMessage chunks from `provider.synthesize_speech(...)`
   to that language's room.

Translated audio is broadcast ONLY to each listener's own private
per-language room -- never played back into the ingest stream itself.
That's what makes this architecture immune to the Phase 10 feedback-loop
class of bug (audio "hearing itself") by construction: there is no path
for synthesized speech to ever become new input.
"""

from __future__ import annotations

import asyncio
import base64
import math
import array
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from backend.audio.segmenter import SegmenterEventKind, SpeechSegmenter
from backend.models.schemas import (
    AudioMessage,
    ErrorMessage,
    MeetingIngestSpeakerMessage,
    MeetingIngestStartMessage,
    StatusMessage,
    StopMessage,
    TranscriptMessage,
    TranslationMessage,
    parse_meeting_ingest_message,
)
from backend.translation.base import TranslationProvider
from backend.translation.factory import get_provider, is_live_provider
from backend.websocket.handlers import _pcm16_to_wav_bytes, _safe_send
from backend.websocket.meeting_registry import MeetingRegistry
from config.languages import is_supported
from config.settings import Settings

logger = logging.getLogger(__name__)

# Sentinel put on the processing queue to tell the consumer task to stop --
# same convention as handlers.py's _STOP.
_STOP = object()

# Providers that implement transcribe_final/translate_final (see
# backend/translation/base.py) -- local/azure inherit the default None
# and would silently produce nothing, so meeting ingest rejects them
# outright at `start` rather than accepting a session that can never work.
_MEETING_INGEST_PROVIDERS = {"gemini", "mock"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rms_int16(pcm16: bytes) -> int:
    """RMS level of a PCM16LE buffer, in raw int16 units (0..32767)."""
    if not pcm16:
        return 0
    samples = array.array("h")
    samples.frombytes(pcm16[: len(pcm16) - (len(pcm16) % 2)])
    if not samples:
        return 0
    return int(math.sqrt(sum(s * s for s in samples) / len(samples)))


def _dump_utterance_audio(settings: Settings, meeting_id: str, audio: bytes, logger: logging.Logger) -> None:
    """
    Write each segmented utterance to a WAV when debug_audio_dump_dir is
    set, so the exact audio the model was given can be listened to.

    This already existed for the single-user path (handlers.py) but not
    for meeting ingest, which is where it is most needed: with a bot in
    the room nobody ever hears what the pipeline actually captured, so a
    hallucinated transcript is impossible to tell apart from a genuine
    mis-hearing without this. Never raises -- debugging aids must not take
    a meeting down.
    """
    if not settings.debug_audio_dump_dir:
        return
    try:
        directory = Path(settings.debug_audio_dump_dir) / f"meeting_{meeting_id}"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"utt_{datetime.now():%Y%m%d_%H%M%S_%f}.wav"
        path.write_bytes(_pcm16_to_wav_bytes(audio, settings.audio_sample_rate))
        logger.debug("Meeting %s: dumped utterance audio to %s", meeting_id, path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Meeting %s: failed to dump utterance audio: %s", meeting_id, exc)


async def _process_utterance(
    provider: TranslationProvider,
    settings: Settings,
    registry: MeetingRegistry,
    meeting_id: str,
    audio: bytes,
    speaker: Optional[str] = None,
) -> None:
    started_at = time.perf_counter()

    # Level of the audio actually handed to the model. Logged on every
    # utterance because a speech model given noise does not fail loudly --
    # it invents fluent, plausible sentences. When transcripts start
    # reading like someone else's conversation, the question is always
    # "was this really speech?", and rms is the cheapest answer.
    utterance_rms = _rms_int16(audio)
    _dump_utterance_audio(settings, meeting_id, audio, logger)

    transcription = await provider.transcribe_final(audio)
    transcribe_s = time.perf_counter() - started_at
    if transcription is None or not transcription.text:
        logger.debug("Meeting %s: empty transcript for a %.1fs utterance (rms=%d)",
                     meeting_id, len(audio) / 2 / 16000, utterance_rms)
        return

    detected_lang = transcription.detected_language
    if detected_lang not in settings.meeting_languages:
        logger.warning(
            "Meeting %s: dropping an utterance in unrecognized/undetected language %r "
            "(configured: %s). Transcript was: %r",
            meeting_id,
            detected_lang,
            ",".join(settings.meeting_languages),
            transcription.text[:120],
        )
        return

    logger.info(
        "Meeting %s: [%s] rms=%d %.1fs -> %r",
        meeting_id,
        detected_lang,
        utterance_rms,
        len(audio) / 2 / 16000,
        transcription.text[:160],
    )

    timestamp = _now_iso()

    # Pass-through transcript, unmodified, to same-language listeners --
    # they hear the live meeting audio directly, so this is captions
    # only. Zero extra provider calls.
    await registry.broadcast(
        meeting_id,
        detected_lang,
        TranscriptMessage(
            text=transcription.text,
            is_final=True,
            timestamp=timestamp,
            detected_language=detected_lang,
            speaker=speaker,
        ).model_dump_json(),
    )

    # Phase 14 latency work. Two structural problems were fixed here.
    #
    # 1. SEQUENTIAL TARGETS. The original loop did, per target language in
    #    turn: translate, then stream the whole TTS, then move to the next
    #    language. So with en/az/ru, a Russian listener waited for the
    #    entire Azerbaijani translation AND its speech synthesis before
    #    their own translation was even requested. Latency grew linearly
    #    with the number of languages, and the last one always lost.
    #    Languages are independent, so they now run concurrently.
    #
    # 2. TRANSLATING FOR NOBODY. Every configured language was translated
    #    and synthesized whether or not a single listener was in that
    #    room. That is wasted latency for the people who ARE listening
    #    (they queue behind those calls) and wasted spend. Phase 11's
    #    premise was "provider cost flat in listeners" -- this makes it
    #    flat in *languages actually being listened to*.
    targets = []
    skipped = []
    for target_lang in settings.meeting_languages:
        if target_lang == detected_lang:
            continue
        if registry.listener_count(meeting_id, target_lang) > 0:
            targets.append(target_lang)
        else:
            skipped.append(target_lang)

    if skipped:
        logger.debug("Meeting %s: no listeners for %s -- not translating", meeting_id, ",".join(skipped))

    async def _translate_and_speak(target_lang: str) -> tuple[str, float, float]:
        t_start = time.perf_counter()
        translated = await provider.translate_final(transcription.text, detected_lang, target_lang)
        translate_s = time.perf_counter() - t_start
        if not translated:
            return target_lang, translate_s, 0.0

        # Text first, audio after: reading the translation is useful
        # immediately, and TTS is much slower than translation.
        await registry.broadcast(
            meeting_id,
            target_lang,
            TranslationMessage(
                text=translated,
                is_final=True,
                source_lang=detected_lang,
                target_lang=target_lang,
                timestamp=timestamp,
                speaker=speaker,
            ).model_dump_json(),
        )

        tts_start = time.perf_counter()
        async for pcm_chunk, sample_rate in provider.synthesize_speech(translated):
            if not pcm_chunk:
                continue
            await registry.broadcast(
                meeting_id,
                target_lang,
                AudioMessage(
                    audio_base64=base64.b64encode(_pcm16_to_wav_bytes(pcm_chunk, sample_rate)).decode("ascii"),
                    sample_rate=sample_rate,
                    timestamp=timestamp,
                ).model_dump_json(),
            )
        return target_lang, translate_s, time.perf_counter() - tts_start

    results = await asyncio.gather(*(_translate_and_speak(t) for t in targets), return_exceptions=True)

    # Timing breakdown, so latency is diagnosed from numbers rather than
    # impressions. transcribe is one shared call; the rest run in parallel,
    # so the wall clock is roughly transcribe + the slowest language.
    parts = []
    for item in results:
        if isinstance(item, BaseException):
            logger.warning("Meeting %s: a target language failed: %s", meeting_id, item)
            continue
        lang, translate_s, tts_s = item
        parts.append(f"{lang}: translate {translate_s:.2f}s + tts {tts_s:.2f}s")
    logger.info(
        "Meeting %s: utterance done in %.2fs (transcribe %.2fs%s)",
        meeting_id,
        time.perf_counter() - started_at,
        transcribe_s,
        ("; " + "; ".join(parts)) if parts else "",
    )


async def handle_meeting_ingest(
    websocket: WebSocket, settings: Settings, registry: MeetingRegistry, meeting_id: str
) -> None:
    provider_name = settings.translation_provider.lower()

    # Phase 15: gemini_live takes a different path entirely -- one live
    # session per listener language, no VAD segmentation on our side. Same
    # wire protocol, so the bot and _dev_stream_meeting_audio.py are
    # unchanged. Dispatched here rather than in main.py so both meeting
    # modes stay behind one endpoint.
    if is_live_provider(provider_name):
        from backend.websocket.live_meeting_handlers import handle_meeting_ingest_live

        await handle_meeting_ingest_live(websocket, settings, registry, meeting_id)
        return

    if provider_name not in _MEETING_INGEST_PROVIDERS:
        await _safe_send(
            websocket,
            ErrorMessage(
                message=(
                    f"Meeting broadcast mode requires TRANSLATION_PROVIDER to be gemini_live or one of "
                    f"{sorted(_MEETING_INGEST_PROVIDERS)} (got '{settings.translation_provider}') -- "
                    "see transcribe_final/translate_final in backend/translation/base.py."
                )
            ).model_dump_json(),
        )
        await websocket.close()
        return

    if not registry.try_claim_ingest(meeting_id):
        await _safe_send(
            websocket,
            ErrorMessage(
                message=f"Meeting '{meeting_id}' already has an active ingest connection"
            ).model_dump_json(),
        )
        await websocket.close()
        return

    segmenter: Optional[SpeechSegmenter] = None
    provider: Optional[TranslationProvider] = None
    session_active = False

    queue: "asyncio.Queue" = asyncio.Queue()
    consumer_task: Optional[asyncio.Task] = None

    # Phase 14 speaker attribution. `current_speaker` is whatever the bot
    # last reported; `utterance_speaker` is that value snapshotted at
    # SPEECH_START. The snapshot matters: an utterance is only segmented
    # after the trailing silence, by which point the Teams roster has
    # frequently already moved on to whoever spoke next, so reading the
    # live value at UTTERANCE_READY attributes lines to the wrong person
    # exactly when conversation is quickest.
    current_speaker: Optional[str] = None
    utterance_speaker: Optional[str] = None

    async def consume() -> None:
        while True:
            item = await queue.get()
            if item is _STOP:
                return
            audio, speaker = item
            try:
                await _process_utterance(provider, settings, registry, meeting_id, audio, speaker)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Meeting %s: error while processing a buffered utterance", meeting_id)

    async def drain_consumer() -> None:
        nonlocal consumer_task
        if consumer_task is None:
            return
        await queue.put(_STOP)
        try:
            await consumer_task
        except Exception:  # noqa: BLE001
            logger.exception("Meeting %s: consumer task raised while draining", meeting_id)
        consumer_task = None

    async def cancel_consumer() -> None:
        nonlocal consumer_task
        if consumer_task is None:
            return
        consumer_task.cancel()
        try:
            await consumer_task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            logger.exception("Meeting %s: consumer task raised while cancelling", meeting_id)
        consumer_task = None

    try:
        while True:
            message = await websocket.receive()

            if message.get("type") == "websocket.disconnect":
                break

            if "text" in message and message["text"] is not None:
                try:
                    parsed = parse_meeting_ingest_message(message["text"])
                except ValidationError as exc:
                    await _safe_send(websocket, ErrorMessage(message=f"Invalid message: {exc}").model_dump_json())
                    continue

                if isinstance(parsed, MeetingIngestStartMessage):
                    segmenter = SpeechSegmenter(
                        sample_rate=parsed.sample_rate,
                        channels=settings.audio_channels,
                        pre_speech_ms=settings.vad_pre_speech_ms,
                        end_silence_ms=settings.vad_end_silence_ms,
                        vad_threshold=settings.vad_threshold,
                        partial_interval_ms=settings.vad_partial_interval_ms,
                    )
                    provider = get_provider(settings)
                    # Placeholder pair -- translate_final always overrides
                    # source_lang/target_lang explicitly per call (see
                    # base.py), so self._source_lang/self._target_lang go
                    # unused for this flow. Not a bug -- just satisfying
                    # start_session's required signature.
                    await provider.start_session("auto", "auto")
                    session_active = True
                    consumer_task = asyncio.create_task(consume())
                    logger.info("Meeting %s: ingest started", meeting_id)
                    await _safe_send(websocket, StatusMessage(status="listening").model_dump_json())

                elif isinstance(parsed, MeetingIngestSpeakerMessage):
                    current_speaker = parsed.name
                    logger.debug("Meeting %s: active speaker is now %r", meeting_id, current_speaker)

                elif isinstance(parsed, StopMessage):
                    if session_active and segmenter is not None and provider is not None:
                        remainder = segmenter.flush()
                        if remainder:
                            queue.put_nowait((remainder, utterance_speaker))
                        await drain_consumer()
                        await provider.close_session()
                    session_active = False
                    logger.info("Meeting %s: ingest stopped", meeting_id)
                    await _safe_send(websocket, StatusMessage(status="connected").model_dump_json())

            elif "bytes" in message and message["bytes"] is not None:
                if not session_active or segmenter is None or provider is None:
                    await _safe_send(
                        websocket, ErrorMessage(message="Received audio before a start message").model_dump_json()
                    )
                    continue

                data = message["bytes"]
                for event in segmenter.push(data):
                    if event.kind == SegmenterEventKind.SPEECH_START:
                        # Snapshot who was talking as this utterance began.
                        utterance_speaker = current_speaker
                    elif event.kind == SegmenterEventKind.UTTERANCE_READY:
                        queue.put_nowait((event.audio, utterance_speaker))
                    # PARTIAL_UPDATE is deliberately ignored -- v1 sends no
                    # interim captions in meeting mode (Phase 11 plan).

    except WebSocketDisconnect:
        logger.info("Meeting %s: ingest connection disconnected", meeting_id)
    except Exception as exc:  # noqa: BLE001 - report to client instead of a bare 500/close
        logger.exception("Meeting %s: unhandled error in ingest session", meeting_id)
        await _safe_send(websocket, ErrorMessage(message=str(exc)).model_dump_json())
    finally:
        await cancel_consumer()
        if session_active and provider is not None:
            try:
                await provider.close_session()
            except Exception:  # noqa: BLE001
                pass
        registry.release_ingest(meeting_id)


async def handle_meeting_listener(
    websocket: WebSocket, settings: Settings, registry: MeetingRegistry, meeting_id: str, lang: str
) -> None:
    if not is_supported(lang):
        await _safe_send(websocket, ErrorMessage(message=f"Unsupported language '{lang}'").model_dump_json())
        await websocket.close()
        return

    registry.add_listener(meeting_id, lang, websocket)
    count = registry.listener_count(meeting_id, lang)
    if count > settings.meeting_max_listeners_per_meeting:
        # Advisory only -- never rejects, see meeting_max_listeners_per_meeting
        # in config/settings.py.
        logger.warning(
            "Meeting %s/%s: listener count %d exceeds the advisory cap of %d",
            meeting_id,
            lang,
            count,
            settings.meeting_max_listeners_per_meeting,
        )
    await _safe_send(websocket, StatusMessage(status="listening").model_dump_json())

    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            # Listeners never send anything meaningful -- this loop exists
            # purely to detect disconnect. handle_meeting_ingest's
            # consume()/_process_utterance is the sole writer to this
            # socket, via registry.broadcast().
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        logger.exception("Meeting %s/%s: unhandled error in listener session", meeting_id, lang)
    finally:
        registry.remove_listener(meeting_id, lang, websocket)

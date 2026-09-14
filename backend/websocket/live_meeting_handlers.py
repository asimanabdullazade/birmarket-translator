"""
Phase 15: meeting broadcast mode powered by Gemini Live.

WHY THIS EXISTS ALONGSIDE meeting_handlers.py
---------------------------------------------
The Phase 11 meeting path chops the incoming stream into phrases with our
own VAD, then per phrase calls transcribe -> translate -> synthesize. That
works, but almost every failure in practice came from the segmentation
layer rather than from the models:

  * a guest joining on a laptop mic peaked at rms ~82 while the host hit
    ~3500, so the quiet speaker fell below vad_threshold and was silently
    never transcribed at all;
  * normalising the capture to fix that pushed room tone above the
    threshold instead, and a speech model handed noise does not fail
    loudly -- it returns fluent invented sentences;
  * latency is inherently at least one full silence timeout plus a
    sequential round trip per language.

Gemini Live does its own endpointing on a continuous stream, so this path
deletes our VAD from meeting mode entirely. No threshold to tune, no
fragments, nothing to normalise.

WHY ONE SESSION PER LISTENER LANGUAGE
-------------------------------------
Live Translate's TranslationConfig takes a target_language_code but has no
source_language_code -- the source is auto-detected (see "Source language"
in live_handlers.py). So a session binds to a TARGET only, and the number
of sessions is the number of distinct languages people are listening in,
not the number of (source, target) pairs. Everyone's audio goes to every
session; each one detects and translates independently.

That keeps Phase 11's economics: cost is flat in listener count and linear
in languages actually being listened to.

RELATIONSHIP TO live_handlers.py
--------------------------------
The single-user path is one websocket <-> one session, with _sender
reading audio straight off the client socket. Meeting mode is the
opposite shape: one ingest socket fanning out to N sessions, each
broadcasting into a different room. The connection/reconnect handling and
the response-parsing rules are the same ideas, but the plumbing is
inverted, so this is a sibling module rather than a reuse of
run_live_session.

TRANSCRIPT ROUTING
------------------
Every session produces its own copy of the input transcript (the
source-language text), so broadcasting all of them would give listeners N
duplicates. One session is designated primary and its input transcript is
broadcast to every room; each session broadcasts only its own translation
and audio to its own room. A listener whose language matches what is being
spoken therefore sees captions and no translation, which is the same
behaviour the Phase 11 path produces.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, Optional

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from backend.models.schemas import (
    AudioMessage,
    ErrorMessage,
    MeetingIngestSpeakerMessage,
    MeetingIngestStartMessage,
    OriginalAudioMessage,
    StatusMessage,
    StopMessage,
    TranscriptMessage,
    TranslationMessage,
    parse_meeting_ingest_message,
)
from backend.websocket.handlers import _pcm16_to_wav_bytes, _safe_send
from backend.websocket.meeting_registry import MeetingRegistry
from config.settings import Settings

logger = logging.getLogger(__name__)

# Gemini Live returns 24kHz PCM, same as the single-user path.
_OUTPUT_SAMPLE_RATE = 24000

# How often to reconcile open sessions against the languages listeners are
# actually in. Listeners join and leave mid-meeting, and a session is only
# worth paying for while somebody is in its room.
_RECONCILE_INTERVAL_S = 2.0

# A speaker change only ends a phrase once the phrase is substantial
# enough that the change is plausibly real rather than indicator noise.
# See the guard in _MeetingLiveSession.run().
_MIN_PHRASE_FOR_SPEAKER_CUT_S = 1.5
_MIN_WORDS_FOR_SPEAKER_CUT = 4


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class _Phrase:
    """One phrase's worth of accumulated text for a single session.

    `speaker` is snapshotted when the phrase OPENS, not when it finalizes:
    by the time Gemini signals the end of a phrase the Teams roster has
    frequently moved on to whoever spoke next, so reading it late
    misattributes lines exactly when conversation is quickest.
    """

    started_at: str = field(default_factory=_now_iso)
    speaker: Optional[str] = None
    input_text: str = ""
    output_text: str = ""
    last_input_activity: float = field(default_factory=lambda: asyncio.get_event_loop().time())
    opened_at: float = field(default_factory=lambda: asyncio.get_event_loop().time())

    def is_empty(self) -> bool:
        return not self.input_text and not self.output_text


class _MeetingLiveSession:
    """One Gemini Live connection, translating the meeting into one target
    language and broadcasting into that language's room."""

    def __init__(
        self,
        target_lang: str,
        settings: Settings,
        registry: MeetingRegistry,
        meeting_id: str,
        speaker_provider: Callable[[], Optional[str]],
        is_primary: bool,
    ) -> None:
        self.target_lang = target_lang
        self.is_primary = is_primary
        self._settings = settings
        self._registry = registry
        self._meeting_id = meeting_id
        self._speaker_provider = speaker_provider
        self._session: Optional[object] = None
        self._stop = False
        # Phrase state lives on the instance so the silence watchdog (see
        # run()) can finalize between turns, not just while responses are
        # arriving. The lock keeps the watchdog and the receive loop from
        # finalizing the same phrase twice.
        self._phrase = _Phrase()
        self._phrase_lock = asyncio.Lock()

    async def feed(self, pcm16: bytes, sample_rate: int) -> None:
        """Forward one audio frame. Dropped silently while reconnecting --
        there are no utterance boundaries here to buffer to, and
        reinventing them is exactly what this module exists to avoid."""
        session = self._session
        if session is None or self._stop:
            return
        try:
            from google.genai import types

            await session.send_realtime_input(  # type: ignore[attr-defined]
                audio=types.Blob(data=pcm16, mime_type=f"audio/pcm;rate={sample_rate}")
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "Meeting %s [%s]: dropped an audio frame, session mid-reconnect",
                self._meeting_id,
                self.target_lang,
                exc_info=True,
            )

    def request_stop(self) -> None:
        self._stop = True

    async def run(self) -> None:
        """Connect, drive, reconnect. Returns when request_stop() is set or
        a fatal access error occurs."""
        from google import genai
        from google.genai import errors as genai_errors
        from google.genai import types

        client = genai.Client(api_key=self._settings.gemini_api_key)

        # Constrain auto-detection to the languages this meeting actually
        # uses. A meeting has no single source language, but that is not
        # the same as having no information: an unconstrained recogniser
        # transcribed Azerbaijani "salam" as Italian "ciao" -- phonetically
        # close, and a perfectly reasonable guess if Italian is on the
        # table at all. language_codes is a plural hint, so the whole
        # configured set can be passed at once.
        #
        # The non-live path hit the identical problem from the other side
        # (Gemini returning detected_language 'fr'/'ko' for az/en speech,
        # and those utterances being dropped) and was fixed the same way:
        # close the set of candidates rather than widen what is accepted
        # downstream.
        hint_languages = list(self._settings.meeting_languages)

        def _build_config(with_hint: bool):
            transcription = (
                types.AudioTranscriptionConfig(language_codes=hint_languages)
                if with_hint
                else types.AudioTranscriptionConfig()
            )
            return types.LiveConnectConfig(
                response_modalities=["AUDIO"],
                input_audio_transcription=transcription,
                output_audio_transcription=types.AudioTranscriptionConfig(),
                translation_config=types.TranslationConfig(
                    target_language_code=self.target_lang, echo_target_language=False
                ),
            )

        # If the installed SDK/model rejects a multi-language hint, fall
        # back to the unconstrained config rather than losing the session
        # entirely -- bad transcripts beat no translation.
        use_hint = True
        live_config = _build_config(True)

        loop = asyncio.get_event_loop()
        self._phrase = _Phrase(speaker=self._speaker_provider())
        finalize_silence_s = self._settings.gemini_live_finalize_silence_ms / 1000
        max_phrase_s = self._settings.gemini_live_max_phrase_seconds
        attempt = 0

        async def finalize(reason: str) -> None:
            phrase = self._phrase
            if not phrase.is_empty():
                input_text = phrase.input_text.strip()
                output_text = phrase.output_text.strip()

                # Only the primary session broadcasts the source-language
                # transcript, and it goes to every room -- see "Transcript
                # routing" in the module docstring.
                if self.is_primary and input_text:
                    payload = TranscriptMessage(
                        text=input_text,
                        is_final=True,
                        timestamp=phrase.started_at,
                        speaker=phrase.speaker,
                    ).model_dump_json()
                    for lang in self._settings.meeting_languages:
                        await self._registry.broadcast(self._meeting_id, lang, payload)

                if output_text:
                    await self._registry.broadcast(
                        self._meeting_id,
                        self.target_lang,
                        TranslationMessage(
                            text=output_text,
                            is_final=True,
                            source_lang="auto",
                            target_lang=self.target_lang,
                            timestamp=phrase.started_at,
                            speaker=phrase.speaker,
                        ).model_dump_json(),
                    )
                logger.info(
                    "Meeting %s [%s]: phrase finalized (%s) speaker=%r %r -> %r",
                    self._meeting_id,
                    self.target_lang,
                    reason,
                    phrase.speaker,
                    input_text[:80],
                    output_text[:80],
                )
            self._phrase = _Phrase(speaker=self._speaker_provider())

        async def locked_finalize(reason: str) -> None:
            async with self._phrase_lock:
                await finalize(reason)

        async def silence_watchdog() -> None:
            """Finalize a phrase that has gone quiet.

            The receive loop only evaluates its finalize conditions when a
            response arrives, and between turns nothing arrives at all --
            so without this, the last sentence of every burst would sit
            unsent until somebody spoke again. That is what made
            turn_complete load-bearing as a finalizer, and finalizing on
            every turn is exactly what chopped sentences into two-word
            fragments.
            """
            while not self._stop:
                await asyncio.sleep(0.25)
                phrase = self._phrase
                if phrase.is_empty():
                    continue
                if (loop.time() - phrase.last_input_activity) >= finalize_silence_s:
                    await locked_finalize("input-silence timeout (watchdog)")

        watchdog = asyncio.create_task(silence_watchdog(), name=f"watchdog-{self.target_lang}")

        while not self._stop:
            attempt += 1
            try:
                async with client.aio.live.connect(
                    model=self._settings.gemini_live_model, config=live_config
                ) as session:
                    if self._stop:
                        return
                    self._session = session
                    logger.info("Meeting %s [%s]: live session connected", self._meeting_id, self.target_lang)

                    # session.receive() ends after each turn_complete, so
                    # this inner loop re-arms it; it is not a reconnect.
                    while not self._stop:
                        async for response in session.receive():
                            if self._stop:
                                return
                            content = response.server_content
                            if content is None:
                                continue

                            phrase = self._phrase
                            if content.input_transcription is not None and content.input_transcription.text:
                                if phrase.is_empty():
                                    # First text of a new phrase: this is
                                    # the moment the speaker is real.
                                    phrase.speaker = self._speaker_provider()
                                phrase.input_text += content.input_transcription.text
                                phrase.last_input_activity = loop.time()
                            if content.output_transcription is not None and content.output_transcription.text:
                                phrase.output_text += content.output_transcription.text

                            if content.model_turn is not None:
                                for part in content.model_turn.parts or []:
                                    inline = part.inline_data
                                    if inline is None or not inline.data:
                                        continue
                                    await self._registry.broadcast(
                                        self._meeting_id,
                                        self.target_lang,
                                        AudioMessage(
                                            audio_base64=base64.b64encode(
                                                _pcm16_to_wav_bytes(inline.data, _OUTPUT_SAMPLE_RATE)
                                            ).decode("ascii"),
                                            sample_rate=_OUTPUT_SAMPLE_RATE,
                                            timestamp=phrase.started_at,
                                            speaker=phrase.speaker,
                                        ).model_dump_json(),
                                    )

                            # A CHANGE OF SPEAKER IS A PHRASE BOUNDARY.
                            #
                            # Without this, a phrase only ends on silence,
                            # turn_complete, or the max-phrase cap -- and a
                            # real back-and-forth has none of those between
                            # turns. One person's phrase then keeps
                            # accumulating the next person's words and
                            # keeps the first person's name, which reads as
                            # "it thinks she is still talking when I talk".
                            #
                            # None means the bot cannot currently tell who
                            # is speaking (normal between turns), so it is
                            # explicitly NOT treated as a change -- doing so
                            # would chop every phrase in half at each brief
                            # gap in the roster indicator.
                            speaker_now = self._speaker_provider()
                            if speaker_now is not None:
                                if phrase.speaker is None:
                                    phrase.speaker = speaker_now
                                elif speaker_now != phrase.speaker and not phrase.is_empty():
                                    # GUARD: only an ESTABLISHED phrase may be
                                    # cut short by a speaker change.
                                    #
                                    # Without this, the roster indicator's
                                    # normal flicker (name -> None -> other
                                    # name, every second or two) finalized a
                                    # phrase every couple of words, and
                                    # captions arrived as "Well," / "that's" /
                                    # "like" instead of sentences. A genuine
                                    # change of speaker is always separated by
                                    # enough speech to clear these; indicator
                                    # noise never is.
                                    long_enough = (loop.time() - phrase.opened_at) >= _MIN_PHRASE_FOR_SPEAKER_CUT_S
                                    wordy_enough = len(phrase.input_text.split()) >= _MIN_WORDS_FOR_SPEAKER_CUT
                                    if long_enough and wordy_enough:
                                        await locked_finalize(f"speaker changed to {speaker_now!r}")
                                        self._phrase.speaker = speaker_now

                            input_finished = bool(
                                content.input_transcription and content.input_transcription.finished
                            )
                            turn_done = bool(content.turn_complete or content.generation_complete)
                            silence_elapsed = not phrase.is_empty() and (
                                loop.time() - phrase.last_input_activity
                            ) >= finalize_silence_s
                            over_cap = (loop.time() - phrase.opened_at) >= max_phrase_s

                            # turn_done is deliberately NOT a trigger by
                            # itself. Gemini Live ends a turn every time it
                            # emits a chunk of response -- several times
                            # per sentence while the speaker is still
                            # talking -- so finalizing on it produced
                            # captions like "Well," / "that's" / "like",
                            # each translated without the context of the
                            # sentence it belonged to. The silence
                            # watchdog above now covers the case turn_done
                            # was really guarding against.
                            if input_finished:
                                await locked_finalize("input_transcription.finished")
                            elif silence_elapsed:
                                await locked_finalize("input-silence timeout")
                            elif over_cap:
                                await locked_finalize("max-phrase cap")

            except asyncio.CancelledError:
                raise
            except genai_errors.ClientError as exc:
                if exc.code in (401, 403, 404):
                    await locked_finalize("fatal access error")
                    logger.error(
                        "Meeting %s [%s]: fatal Gemini Live access error (%s): %s. Check GEMINI_API_KEY "
                        "and access to %s -- it is a preview model and may be gated separately.",
                        self._meeting_id,
                        self.target_lang,
                        exc.code,
                        exc,
                        self._settings.gemini_live_model,
                    )
                    self._stop = True
                    return
                if exc.code == 400 and use_hint:
                    logger.warning(
                        "Meeting %s [%s]: the model rejected the language hint %s (%s). Retrying "
                        "without it -- transcripts may pick the wrong language, but translation "
                        "still works.",
                        self._meeting_id,
                        self.target_lang,
                        hint_languages,
                        exc,
                    )
                    use_hint = False
                    live_config = _build_config(False)
                else:
                    logger.warning(
                        "Meeting %s [%s]: request error on attempt #%d (retrying): %s",
                        self._meeting_id,
                        self.target_lang,
                        attempt,
                        exc,
                    )
            except Exception:  # noqa: BLE001
                if self._stop:
                    logger.debug("Meeting %s [%s]: session closed as part of a clean stop",
                                 self._meeting_id, self.target_lang, exc_info=True)
                else:
                    logger.exception(
                        "Meeting %s [%s]: session ended unexpectedly on attempt #%d (retrying)",
                        self._meeting_id,
                        self.target_lang,
                        attempt,
                    )
            finally:
                self._session = None

            if self._stop:
                break
            await locked_finalize("reconnecting")
            backoff_s = min(attempt * 1.0, 5.0)
            logger.info(
                "Meeting %s [%s]: reconnecting in %.0fs", self._meeting_id, self.target_lang, backoff_s
            )
            await asyncio.sleep(backoff_s)

        watchdog.cancel()
        await locked_finalize("session stopped")


class _SessionPool:
    """Keeps exactly one live session open per language that has listeners."""

    def __init__(
        self,
        settings: Settings,
        registry: MeetingRegistry,
        meeting_id: str,
        speaker_provider: Callable[[], Optional[str]],
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._meeting_id = meeting_id
        self._speaker_provider = speaker_provider
        self._sessions: Dict[str, _MeetingLiveSession] = {}
        self._tasks: Dict[str, asyncio.Task] = {}

    def _wanted_languages(self) -> list[str]:
        return [
            lang
            for lang in self._settings.meeting_languages
            if self._registry.listener_count(self._meeting_id, lang) > 0
        ]

    async def reconcile(self) -> None:
        wanted = set(self._wanted_languages())
        current = set(self._sessions)

        for lang in wanted - current:
            # Exactly one session is primary, so the source-language
            # transcript is broadcast once rather than once per language.
            is_primary = not any(s.is_primary for s in self._sessions.values())
            session = _MeetingLiveSession(
                lang, self._settings, self._registry, self._meeting_id, self._speaker_provider, is_primary
            )
            self._sessions[lang] = session
            self._tasks[lang] = asyncio.create_task(session.run(), name=f"live-{self._meeting_id}-{lang}")
            logger.info("Meeting %s: opening live session for %r (primary=%s)", self._meeting_id, lang, is_primary)

        for lang in current - wanted:
            logger.info("Meeting %s: no listeners left for %r -- closing its session", self._meeting_id, lang)
            await self._close(lang)

        # If the primary went away, promote another so transcripts keep
        # flowing.
        if self._sessions and not any(s.is_primary for s in self._sessions.values()):
            next(iter(self._sessions.values())).is_primary = True

    async def _close(self, lang: str) -> None:
        session = self._sessions.pop(lang, None)
        task = self._tasks.pop(lang, None)
        if session is not None:
            session.request_stop()
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def feed(self, pcm16: bytes, sample_rate: int) -> None:
        if not self._sessions:
            return
        await asyncio.gather(
            *(s.feed(pcm16, sample_rate) for s in self._sessions.values()), return_exceptions=True
        )

    async def close_all(self) -> None:
        for lang in list(self._sessions):
            await self._close(lang)


async def handle_meeting_ingest_live(
    websocket: WebSocket, settings: Settings, registry: MeetingRegistry, meeting_id: str
) -> None:
    """Gemini Live variant of handle_meeting_ingest. Same wire protocol --
    start / binary PCM / speaker / stop -- so the bot and the dev WAV
    streamer need no changes at all."""
    if not registry.try_claim_ingest(meeting_id):
        await _safe_send(
            websocket,
            ErrorMessage(message=f"Meeting {meeting_id} already has an active ingest connection").model_dump_json(),
        )
        await websocket.close()
        return

    current_speaker: Optional[str] = None
    pool = _SessionPool(settings, registry, meeting_id, lambda: current_speaker)
    reconcile_task: Optional[asyncio.Task] = None
    sample_rate = settings.audio_sample_rate
    session_active = False

    async def reconcile_loop() -> None:
        while True:
            try:
                await pool.reconcile()
            except Exception:  # noqa: BLE001
                logger.exception("Meeting %s: session reconcile failed", meeting_id)
            await asyncio.sleep(_RECONCILE_INTERVAL_S)

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
                    sample_rate = parsed.sample_rate
                    session_active = True
                    await pool.reconcile()
                    reconcile_task = asyncio.create_task(reconcile_loop(), name=f"reconcile-{meeting_id}")
                    logger.info(
                        "Meeting %s: live ingest started (%d listener languages active)",
                        meeting_id,
                        len(pool._sessions),
                    )
                    await _safe_send(websocket, StatusMessage(status="listening").model_dump_json())

                elif isinstance(parsed, MeetingIngestSpeakerMessage):
                    current_speaker = parsed.name
                    logger.debug("Meeting %s: active speaker is now %r", meeting_id, current_speaker)

                elif isinstance(parsed, StopMessage):
                    session_active = False
                    if reconcile_task is not None:
                        reconcile_task.cancel()
                        reconcile_task = None
                    await pool.close_all()
                    logger.info("Meeting %s: live ingest stopped", meeting_id)
                    await _safe_send(websocket, StatusMessage(status="connected").model_dump_json())

            elif "bytes" in message and message["bytes"] is not None:
                if not session_active:
                    await _safe_send(
                        websocket, ErrorMessage(message="Received audio before a start message").model_dump_json()
                    )
                    continue
                await pool.feed(message["bytes"], sample_rate)

                # Relay the untranslated audio to every listener room so
                # clients can mix original against translation. Off by
                # default: it roughly doubles per-listener bandwidth and
                # is only useful to someone who has muted Teams.
                if settings.relay_original_audio:
                    payload = OriginalAudioMessage(
                        audio_base64=base64.b64encode(
                            _pcm16_to_wav_bytes(message["bytes"], sample_rate)
                        ).decode("ascii"),
                        sample_rate=sample_rate,
                        timestamp=_now_iso(),
                    ).model_dump_json()
                    for lang in settings.meeting_languages:
                        if registry.listener_count(meeting_id, lang) > 0:
                            await registry.broadcast(meeting_id, lang, payload)

    except WebSocketDisconnect:
        logger.info("Meeting %s: live ingest connection disconnected", meeting_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Meeting %s: unhandled error in live ingest session", meeting_id)
        await _safe_send(websocket, ErrorMessage(message=str(exc)).model_dump_json())
    finally:
        if reconcile_task is not None:
            reconcile_task.cancel()
        await pool.close_all()
        registry.release_ingest(meeting_id)

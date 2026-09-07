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
   Immediately after a final translation is sent, its text is also handed
   to `provider.synthesize_speech()` (Step 6) -- each synthesized audio
   chunk is streamed to the client as its own `audio` message as soon as
   it's ready (not batched), so playback of the first chunk can start
   before later ones finish synthesizing. This is skipped entirely while
   the client has sent `set_muted: true`, saving the TTS call.
4. Client sends `stop`: server flushes whatever phrase was still in
   progress (as a final), lets the background task finish processing
   whatever's already queued (the socket is still open, so it's worth
   sending final results), closes the provider session, sends any final
   events, then `status: connected`.
5. Client disconnects unexpectedly: server *cancels* the background task
   instead of draining it -- see "Drain vs. cancel" below.
6. Client sends `set_muted` at any point during an active session to
   toggle Step 6's speech synthesis on/off server-side. Independent of the
   client's own playback volume/mute (see "Translation audio playback" in
   the README) -- this one is purely about not spending a TTS call on
   audio the client has already said it won't play.
7. Client sends `audio_played` the instant it starts playing the first
   synthesized audio chunk of a phrase (Step 7). Combined with the
   timestamps this module already records for that phrase (VAD end,
   transcript, translation, first TTS chunk ready), that's enough to log a
   complete speech-start-to-audio-heard latency breakdown -- see
   "Latency breakdown" below and "Measuring latency" in the README.

One provider is a structural exception to all of the above: for
`TRANSLATION_PROVIDER=gemini_live`, a validated `start` message is handed
off whole to `run_live_session` (backend/websocket/live_handlers.py)
instead of going through the segmenter/queue/consumer pipeline described
here -- see that module's docstring for why (no utterance boundaries to
segment in the first place) and "Live-mode dispatch" below for exactly
where the fork happens.

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

Live-mode dispatch
------------------
`is_live_provider(settings.translation_provider)` (backend/translation/
factory.py) is checked in the `StartMessage` branch below immediately
after the language-support validation both modes share, and before this
module builds a `SpeechSegmenter`/calls `get_provider()` -- so that shared
prelude (accept, `status: connected`, receive+validate `start`) lives in
exactly one place for every provider, and only forks into two paths after
it succeeds. `run_live_session` owns the rest of that session's lifecycle
entirely (including reading `stop`/`set_muted` off the same socket) and
returns once it ends, at which point this module's outer loop resumes
normally -- so a second `start` on the same socket still works, same as
every other provider.

And independent of that, VAD phrase boundaries are logged at INFO level
(speech started / an utterance of N seconds was queued), and setting
DEBUG_AUDIO_DUMP_DIR (see config/settings.py) records both the whole
session's raw audio (raw.wav) and each individual detected *final* phrase
(utterance_001.wav, utterance_002.wav, ...) to .wav files -- the latter is
the direct way to check VAD is placing phrase boundaries correctly (not
clipping the first word, not fragmenting a sentence on a short pause).

Streaming translation (Phase 8)
--------------------------------
For gemini/mock (the only providers that implement
TranslationProvider.translate_partial -- local/azure are unaffected, see
"Using streaming translation" in the README), a *second* thing happens
inside the `"partial"` branch below, independent of the transcript-only
logic Step 4 already does there: each new partial transcript is compared
word-by-word against the *previous* one (see backend/translation/
stability.py's "local agreement" helpers) to find a stable, unlikely-to-
change prefix. Once that prefix has grown enough, the newly-stabilized
increment (never anything already committed) is translated and spoken
immediately -- via the same `_stream_tts` used for finals -- rather than
waiting for the whole phrase to end. This state lives in
`phrase_streaming_state`, keyed by each phrase's `started_at` (the same
pattern `latency` already uses) rather than shared nonlocals reset on
SPEECH_START, specifically because `consume()` can lag behind the main
receive loop (see "Why a queue" above) -- a new phrase's SPEECH_START can
arrive before the previous phrase's queued items finish, and resetting
shared state on SPEECH_START would corrupt that still-in-flight phrase's
commit state.

At finalization (the `"final"` branch), the authoritative
process_audio_chunk() transcript/translation are computed exactly as
before Phase 8 -- this alone is what "corrects" the transcript shown to
the user, since that full re-transcription naturally fixes any earlier
partial mistake, no extra code needed. For *audio*, only the part of the
final translation not already covered by what was incrementally committed
gets synthesized (reconcile_final_tts_text in stability.py) -- so a phrase
that streamed cleanly never re-speaks its own already-played audio. See
that function's docstring for why this reconciliation is deliberately an
all-or-nothing decision rather than a fine-grained diff.

Bugfix (found during manual testing): the authoritative final translation
is what's supposed to correct anything shown so far for a phrase -- but if
it comes back completely EMPTY (a short trailing bit of audio VAD
segmented as its own phrase, which the authoritative pass correctly
recognizes as no real speech) while something WAS incrementally committed
and already displayed, nothing else ever corrected it, leaving a
translation with no matching source text stuck on screen. See the explicit
empty-final-translation handling in the "final" branch below.

Latency breakdown (Step 7)
---------------------------
`latency` (a dict, keyed by each phrase's `started_at` timestamp -- the
same one shared by every message about that phrase) accumulates one
extra timestamp per pipeline stage as that phrase moves through this
module: `audio_captured_at` (VAD decided the utterance is complete,
recorded in `enqueue_final`), `transcript_generated_at` /
`translation_generated_at` (recorded right after `process_audio_chunk()`
returns, in `consume()` -- or earlier, per-event, for a provider that can
report its own sub-step timing; see TranslationEvent.generated_at in
base.py), and `voice_ready_at` (the first synthesized audio chunk was
ready, recorded in `_stream_tts`). The phrase's own `started_at`
timestamp doubles as the "user starts speaking" point -- it's set the
moment VAD detects speech beginning.

That's every stage except the very last one, "audio reaches listener",
which can only be known client-side (decoding + Web Audio scheduling all
happen in the browser). The client reports it back explicitly via an
`audio_played` message the instant it starts playing the first chunk of
a phrase (see AudioPlayedMessage in schemas.py) -- at which point
`_log_latency_breakdown` has every timestamp it needs and logs the full
breakdown, in the same shape as the worked example in the Step 7 task:
capture (mic + VAD's deliberate end-of-speech wait), speech recognition,
translation, voice generation, and network+playback, plus a total. See
"Measuring latency" in the README for how to read it and its one real
caveat (frontend and backend clocks are only directly comparable because
they're the same machine's clock in this project's dev setup).
"""

from __future__ import annotations

import asyncio
import base64
import io
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
    AudioMessage,
    AudioPlayedMessage,
    ErrorMessage,
    SetMutedMessage,
    StartMessage,
    StatusMessage,
    StopMessage,
    TranscriptMessage,
    TranslationMessage,
    parse_client_message,
)
from backend.translation.base import EventKind, TranslationEvent, TranslationProvider
from backend.translation.factory import get_provider, is_live_provider
from backend.translation.stability import (
    longest_common_prefix_len,
    reconcile_final_tts_text,
    stable_prefix_len,
    tokenize,
)
from backend.websocket.live_handlers import run_live_session
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


def _pcm16_to_wav_bytes(pcm16_bytes: bytes, sample_rate: int, channels: int = 1) -> bytes:
    """Wrap raw PCM16LE audio in a WAV header, in memory -- so the client
    can hand it straight to the browser's decodeAudioData instead of
    needing to know the sample rate/format out of band (Step 6)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)  # PCM16
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16_bytes)
    return buf.getvalue()


async def _stream_tts(
    websocket: WebSocket, provider: TranslationProvider, text: str, timestamp: str, latency: dict
) -> None:
    """Synthesize `text` (a final translation) to speech and stream each
    ready chunk to the client immediately as an `audio` message (Step 6),
    rather than collecting the whole thing first -- see
    TranslationProvider.synthesize_speech in base.py for why this is a
    streaming call. A synthesis failure is logged and simply means less
    audio gets played; it never propagates up and breaks the rest of the
    pipeline (transcription/translation already succeeded by the time
    this runs).

    Step 7: the moment the *first* chunk is ready (before it's even sent)
    is recorded into `latency[timestamp]["voice_ready_at"]` -- the end of
    the "Voice generation" leg of the latency breakdown. Later chunks of
    the same phrase don't get their own timestamp; only the first chunk's
    readiness (and, client-side, the first chunk's playback) is what the
    breakdown measures.

    Phase 8: this can now be called more than once per phrase (once per
    incremental commit, plus once for the final remainder -- see
    "Streaming translation" above), so "first chunk" is checked against
    the shared `latency` dict rather than a call-local flag -- otherwise a
    later call would overwrite voice_ready_at with a later time instead of
    leaving the true first chunk's timestamp in place. No lock needed:
    consume() is a single task, so there's no concurrent writer for the
    same phrase."""
    try:
        async for pcm_chunk, sample_rate in provider.synthesize_speech(text):
            if not pcm_chunk:
                continue
            entry = latency.setdefault(timestamp, {})
            if "voice_ready_at" not in entry:
                entry["voice_ready_at"] = _now_iso()
            payload = AudioMessage(
                audio_base64=base64.b64encode(_pcm16_to_wav_bytes(pcm_chunk, sample_rate)).decode("ascii"),
                sample_rate=sample_rate,
                timestamp=timestamp,
            ).model_dump_json()
            if not await _safe_send(websocket, payload):
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Error while synthesizing/streaming translated speech")


def _iso_to_epoch_ms(iso_timestamp: str) -> float:
    return datetime.fromisoformat(iso_timestamp).timestamp() * 1000


def _log_latency_breakdown(phrase_timestamp: str, entry: dict, played_at_ms: float) -> None:
    """Step 7: log a start-to-finish latency breakdown for one phrase,
    once the client's `audio_played` message supplies the one timestamp
    that can't be known server-side. See "Latency breakdown" in this
    module's docstring for where each of `entry`'s timestamps comes from,
    and "Measuring latency" in the README for how to read the result.

    Legs with a missing endpoint (e.g. a provider that never populated
    `audio_captured_at` for some reason) are logged as "n/a" rather than
    raising -- this is a diagnostic, not something that should ever take
    down the session."""
    try:
        points_iso = {
            "speech_start": phrase_timestamp,
            "audio_captured_at": entry.get("audio_captured_at"),
            "transcript_generated_at": entry.get("transcript_generated_at"),
            "translation_generated_at": entry.get("translation_generated_at"),
            "voice_ready_at": entry.get("voice_ready_at"),
        }
        points_ms = {key: (_iso_to_epoch_ms(value) if value else None) for key, value in points_iso.items()}
        points_ms["audio_played_at"] = played_at_ms

        order = [
            "speech_start",
            "audio_captured_at",
            "transcript_generated_at",
            "translation_generated_at",
            "voice_ready_at",
            "audio_played_at",
        ]
        labels = [
            "Capture (mic + VAD end-silence wait)",
            "Speech recognition",
            "Translation",
            "Voice generation (TTS)",
            "Network + playback start",
        ]

        lines = [f"Latency breakdown for the phrase starting at {phrase_timestamp}:"]
        for label, start_key, end_key in zip(labels, order[:-1], order[1:]):
            start_ms, end_ms = points_ms[start_key], points_ms[end_key]
            value = f"{end_ms - start_ms:6.0f} ms" if start_ms is not None and end_ms is not None else "   n/a"
            lines.append(f"  {label:<38} {value}")
        lines.append(f"  {'-' * 48}")
        total = points_ms["audio_played_at"] - points_ms["speech_start"]
        lines.append(f"  {'Total (speech start -> audio heard)':<38} {total:6.0f} ms")
        logger.info("\n".join(lines))
    except Exception:
        logger.exception("Failed to compute/log the Step 7 latency breakdown")


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
    # Step 6: skip calling the TTS provider entirely while the client has
    # muted translation audio, rather than synthesizing speech nobody will
    # hear. Set via a `set_muted` message, independent of `start`/`stop`.
    translation_muted = False
    # Step 7: per-phrase latency timestamps, keyed by that phrase's
    # started_at -- see "Latency breakdown" in this module's docstring.
    # Entries are removed once a matching `audio_played` message lets us
    # log the full breakdown; an entry for a phrase that's muted, fails to
    # synthesize, or otherwise never produces audio simply never gets
    # cleaned up early, but that's a handful of small dicts at most for
    # the life of one session -- not worth adding eviction logic for.
    latency: dict = {}
    # Phase 8 (streaming translation): per-in-flight-phrase incremental-
    # commit state, keyed by that phrase's started_at -- same rationale as
    # `latency` above (consume() can lag behind the main receive loop, so
    # keying by started_at rather than resetting shared nonlocals on
    # SPEECH_START keeps each phrase's state independent of processing
    # order/backlog). Each entry: {"last_partial_words": [...],
    # "committed_words": [...], "committed_translation_text": "...",
    # "first_commit_at": Optional[str]}. Popped once that phrase's "final"
    # item finishes processing -- see "Streaming translation" above.
    phrase_streaming_state: dict = {}

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

                    # Phase 8: react to the growing transcript, independent
                    # of whether it changed since the last one *sent* above
                    # -- see "Streaming translation" in this module's
                    # docstring. No-ops for any provider that doesn't
                    # override translate_partial (local/azure), since
                    # translate_partial then just returns None below.
                    if settings.streaming_incremental_translation and text:
                        state = phrase_streaming_state.setdefault(
                            started_at,
                            {
                                "last_partial_words": [],
                                "committed_words": [],
                                "committed_translation_text": "",
                                "first_commit_at": None,
                            },
                        )
                        words = tokenize(text)
                        lcp_len = longest_common_prefix_len(state["last_partial_words"], words)
                        state["last_partial_words"] = words
                        new_stable_len = stable_prefix_len(
                            len(state["committed_words"]),
                            lcp_len,
                            settings.streaming_stability_holdback_words,
                            settings.streaming_min_commit_words,
                        )
                        if new_stable_len > len(state["committed_words"]):
                            new_stable_text = " ".join(words[len(state["committed_words"]) : new_stable_len])
                            translation_delta = await provider.translate_partial(
                                new_stable_text, state["committed_translation_text"]
                            )
                            if translation_delta:
                                state["committed_words"] = words[:new_stable_len]
                                state["committed_translation_text"] = (
                                    f"{state['committed_translation_text']} {translation_delta}".strip()
                                )
                                if state["first_commit_at"] is None:
                                    state["first_commit_at"] = _now_iso()
                                    elapsed_ms = _iso_to_epoch_ms(state["first_commit_at"]) - _iso_to_epoch_ms(
                                        started_at
                                    )
                                    logger.info(
                                        "Streaming translation: first incremental commit at %.0fms into the "
                                        "phrase -- new stable text %r",
                                        elapsed_ms,
                                        new_stable_text,
                                    )
                                await _safe_send(
                                    websocket,
                                    TranslationMessage(
                                        text=state["committed_translation_text"],
                                        is_final=False,
                                        source_lang=source_lang,
                                        target_lang=target_lang,
                                        timestamp=started_at,
                                    ).model_dump_json(),
                                )
                                if not translation_muted:
                                    # Only the new delta -- never re-synthesize
                                    # anything already committed/spoken.
                                    await _stream_tts(websocket, provider, translation_delta, started_at, latency)
                    continue

                # kind == "final"
                if not await _safe_send(websocket, StatusMessage(status="translating").model_dump_json()):
                    continue
                events = await provider.process_audio_chunk(audio)
                await _send_events(websocket, events, target_lang, source_lang, started_at)
                last_partial_text = None

                # Phase 8: whatever got incrementally committed for this
                # phrase (if anything -- empty for providers that don't
                # implement translate_partial, or a phrase too short for
                # any commit to clear the bar) is reconciled against the
                # authoritative final translation below, once it's known.
                streaming_state = phrase_streaming_state.pop(started_at, None)
                committed_translation_text = (
                    streaming_state["committed_translation_text"] if streaming_state else ""
                )

                # Step 7: record when the transcript/translation for this
                # phrase actually became available -- from the event
                # itself if the provider reported it (local_provider.py),
                # otherwise "now" (gemini/mock produce both atomically in
                # one call, so both legitimately share the same instant).
                now = _now_iso()
                latency_entry = latency.setdefault(started_at, {})
                transcript_event = next((e for e in events if e.kind == EventKind.TRANSCRIPT), None)
                translation_event = next((e for e in events if e.kind == EventKind.TRANSLATION), None)
                if transcript_event is not None:
                    latency_entry["transcript_generated_at"] = transcript_event.generated_at or now
                if translation_event is not None:
                    latency_entry["translation_generated_at"] = translation_event.generated_at or now

                translation_text = translation_event.text if translation_event is not None else ""

                # Phase 8 bugfix: the authoritative final translation is
                # supposed to correct anything shown so far for this phrase
                # ("Correct final transcript internally without replaying
                # everything" above) -- but if the final translation comes
                # back EMPTY while something WAS incrementally committed
                # (e.g. a short trailing bit of audio VAD segmented as its
                # own phrase, which the authoritative pass correctly
                # recognizes as no real speech -- process_audio_chunk
                # returns [] whenever its transcript is empty, so
                # translation_event is None and translation_text is "" too
                # here), nothing else ever corrects the display: _send_events
                # above sent nothing (there were no events), so the client is
                # left showing a translation with no matching source text
                # forever. Explicitly clear it with an empty final
                # TranslationMessage whenever this happens, regardless of
                # mute -- this is about text correctness, not the
                # audio-skipping mute toggle below.
                if not translation_text and committed_translation_text:
                    logger.info(
                        "Streaming translation: finalization diverged -- the final translation "
                        "was empty, clearing the incrementally-committed text shown for this phrase"
                    )
                    await _safe_send(
                        websocket,
                        TranslationMessage(
                            text="",
                            is_final=True,
                            source_lang=source_lang,
                            target_lang=target_lang,
                            timestamp=started_at,
                        ).model_dump_json(),
                    )

                if not translation_muted:
                    if translation_text:
                        # Phase 8: speak only what wasn't already spoken
                        # incrementally -- see reconcile_final_tts_text in
                        # stability.py. For any phrase with nothing
                        # committed (committed_translation_text == ""),
                        # this returns translation_text unchanged --
                        # byte-identical to pre-Phase-8 behavior.
                        remainder = reconcile_final_tts_text(
                            committed_translation_text,
                            translation_text,
                            settings.streaming_final_reconcile_min_coverage,
                        )
                        if committed_translation_text:
                            logger.info(
                                "Streaming translation: finalization %s",
                                "spoke only the new remainder"
                                if remainder != translation_text
                                else "diverged from the committed prefix -- spoke the whole final translation",
                            )
                        if remainder:
                            await _stream_tts(websocket, provider, remainder, started_at, latency)

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
        # Step 7: this is "audio reaches backend" in the sense that matters
        # for latency -- the earliest point at which this phrase's *complete*
        # audio is actually available to hand to a provider.
        latency.setdefault(phrase_started_at, {})["audio_captured_at"] = _now_iso()
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

                    if is_live_provider(settings.translation_provider):
                        # See "Live-mode dispatch" above -- this owns the
                        # whole session lifecycle itself (including its
                        # own stop/set_muted handling) and only returns
                        # once that's over, at which point the outer loop
                        # below just resumes waiting for the next message.
                        await run_live_session(websocket, settings, source_lang, target_lang, parsed.sample_rate)
                        continue

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
                    translation_muted = False
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

                elif isinstance(parsed, SetMutedMessage):
                    translation_muted = parsed.muted
                    logger.info("Translation audio %s", "muted" if translation_muted else "unmuted")

                elif isinstance(parsed, AudioPlayedMessage):
                    entry = latency.pop(parsed.timestamp, None)
                    if entry is not None:
                        _log_latency_breakdown(parsed.timestamp, entry, parsed.played_at_ms)

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

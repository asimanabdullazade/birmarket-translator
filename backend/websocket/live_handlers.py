"""
Gemini Live Translate: a second, parallel connection-handling path for
`TRANSLATION_PROVIDER=gemini_live`.

Why this file exists (and isn't just another `TranslationProvider`)
---------------------------------------------------------------------
Every provider in backend/translation/ (gemini_provider.py, azure_provider.py,
local_provider.py, mock_provider.py) is shaped the same way: hand it one
*complete* utterance (already cut to size by our own VAD, see
backend/audio/segmenter.py), get one atomic transcript+translation result
back. That request/response shape has a multi-second floor no amount of
tuning removes -- see the Step 7 latency investigation in the project
history: Gemini's Interactions API measured ~20-27s isolated, Azure's
purpose-built TranslationRecognizer ~6.8s isolated after optimization.
Both wait for the whole utterance, send it, wait for the whole answer.

`gemini-3.5-live-translate-preview` (the *Live* API, a different product
surface than the Interactions API `gemini_provider.py` uses) is
architecturally different: one persistent bidirectional connection,
continuous audio in, continuous translated audio out, translation starting
while the person is still talking. There's no utterance boundary at all on
the way in -- forcing this into `TranslationProvider.process_audio_chunk`
would mean buffering a full VAD phrase before ever talking to the Live
session, which throws away the entire reason to use it. So this mode
bypasses `TranslationProvider`/`factory.py` entirely: `handlers.py` detects
`is_live_provider(settings.translation_provider)` (see factory.py) right
after its existing `start`-message validation and dispatches straight to
`run_live_session` below, instead of building a segmenter + provider +
queue like every other mode.

Two tasks, one persistent connection, `asyncio.TaskGroup`-supervised
--------------------------------------------------------------------
`run_live_session` runs exactly two tasks for the life of one
`start`...`stop` cycle:

- `_sender`: the single, *continuous* reader of the client `websocket` for
  the whole cycle -- it does not get recreated across reconnects (see
  below), so the client's mic stream is never interrupted by one. Forwards
  every binary audio frame straight into whichever Gemini session happens
  to be live right now (no VAD, no buffering), and is also the only place
  reading `stop`/`set_muted` control messages for this mode.
- `_receiver`: the only writer to `websocket`, and the only task that ever
  calls `client.aio.live.connect(...)`. It owns an *internal* reconnect
  loop -- Gemini Live sessions have historically had a hard time limit
  (documented as ~2 minutes for earlier Live models; unconfirmed for this
  one) that will be hit routinely, not as a rare edge case -- so it can
  open, drive, and replace the underlying Gemini connection any number of
  times without `_sender` ever needing to know or being restarted. The two
  tasks share one small `_SharedState` instance: `_receiver` publishes the
  currently-connected session into `shared.session` (None while
  reconnecting); `_sender` just checks that before every send and drops
  the frame if there's nothing to send it to (see "Reconnection" below for
  why dropping, not buffering, is the right call here).

Using `asyncio.TaskGroup` (not two bare `create_task` calls) is
deliberate: if either task raises an unhandled exception, the other is
cancelled automatically and `run_live_session` still returns cleanly
(after reporting the error) instead of leaking a half-finished session --
see `run_live_session`'s `except*` handling.

Turning a continuous stream into discrete phrases
--------------------------------------------------
The frontend/wire protocol (backend/models/schemas.py) still deals in
discrete final transcript/translation messages, same as every other
provider -- `_receiver` is what turns Gemini's continuous stream of
transcription/audio deltas back into that shape. Introspecting the
installed `google-genai` SDK (2.21.0) while building this confirmed the
response shape has several genuinely useful native signals, in priority
order:

1. `response.server_content.input_transcription.finished` (bool) -- the
   API's own signal that transcription of the current input segment is
   done. This is the best available proxy for "the speaker finished this
   phrase" and is checked first.
2. `response.server_content.turn_complete` / `.generation_complete` (bool)
   -- the model has finished generating its response for this turn. A
   coarser signal than #1 (tied to the model's output, not the speaker's
   input) but still a hard guarantee nothing more is coming for this turn.
3. Fallback, whenever neither of the above fires: silence in *input*
   transcription activity exceeding `settings.gemini_live_finalize_silence_ms`
   (default 700ms) -- silence in what the speaker said is a much closer
   proxy for "they paused" than silence in the model's output, which is
   decoupled from the speaker by generation/translation latency.
4. Hard safety cap regardless of the above:
   `settings.gemini_live_max_phrase_seconds` (default 15s) -- so a missed
   signal or an unusually long monologue can't freeze a partial phrase
   forever.

None of #1/#2 being genuinely emitted by `gemini-3.5-live-translate-preview`
specifically (as opposed to being valid fields on the general Live API
response schema, which is all that could be confirmed without a real API
key) is independently verified -- see "Using Gemini Live Translate" in the
README for what to check in your own logs, and tune #3/#4's defaults from
what you actually see.

On any finalize (native, fallback, or the safety cap), *and* right before
a reconnect or on a clean `stop`, whatever text has accumulated is sent as
one final `TranscriptMessage`/`TranslationMessage` pair (skipped if empty)
before the phrase state resets -- so a reconnect or session-death never
leaves the client with a partial phrase it can never learn the end of.

Reconnection
------------
v1 scope is a **cold reconnect**, not gapless resumption: on the
`session.receive()` loop raising or the connection closing, `_receiver`
force-finalizes whatever was in flight, opens a fresh
`client.aio.live.connect(...)`, and keeps going, with a short linear
backoff between attempts. A brief gap/glitch right at that boundary is
accepted for v1 and is a known limitation (gapless resume via Gemini's
session-resumption-handle mechanism is real but adds real complexity that
isn't worth taking on before seeing whether reconnect gaps are actually
bad enough in practice to justify it).

Errors are split into fatal vs. retryable: a `google.genai.errors.ClientError`
with a 401/403/404 status (auth/permission/not-found -- this is a preview
model, so access might be gated separately even under an otherwise-valid
key) is treated as unrecoverable: `_receiver` reports one clear
`ErrorMessage` and raises `_FatalLiveError`, which `run_live_session`
lets cancel `_sender` and end the session instead of reconnecting forever
into a guaranteed repeat failure. Everything else (network blips,
`ServerError`, the connection just closing) is treated as retryable.

Because `_receiver` reconnects on its own initiative regardless of mute
state, "unmute after a long mute reconnects if the session died
server-side" (a scenario the project plan called out explicitly) falls
out for free: `_sender` only checks whether `shared.session` is currently
non-None before forwarding, so if a mute-period reconnect is still
in-flight when the user unmutes, audio is simply dropped (not buffered)
until the next connection is ready -- consistent with how this mode
already drops (never buffers) audio whenever there's no live session to
send it to.

Mute
----
While muted, `_sender` stops forwarding audio into the Live session
*entirely* (unlike every other provider, where muting only discards
already-synthesized output client-side/skips a TTS call -- see mock/gemini/
azure providers' handling of `set_muted`). Gemini Live bills by audio
duration processed as it streams in, so there's real ongoing cost to
sending audio nobody will hear the translation of; not sending it at all
is the correct behavior here in a way it isn't for a per-utterance API.

Source language
---------------
Live Translate's `TranslationConfig` has a `target_language_code` field
but no `source_language_code` -- source language is auto-detected,
confirmed by introspecting the installed SDK's type definitions (not
assumed). `input_audio_transcription`'s `language_codes` field is real,
but is documented as a *hint* for auto-detection, not an override -- it's
passed through as a best-effort nudge using the user's dropdown
selection, but the selection is not enforced. A one-line runtime notice is
sent via the existing `StatusMessage.detail` field (already used for the
`error` status elsewhere; `StatusIndicator` in the frontend already
renders `detail` when present -- no frontend change needed) so this is
visible in the UI rather than buried in a README nobody reads mid-session.

Latency
-------
Step 7's five-leg breakdown (backend/websocket/handlers.py) assumes one
sequential boundary per phrase (capture done -> recognized -> translated
-> synthesized -> played); that doesn't hold for a pipelined stream where
translation starts before the speaker has even finished. This mode
doesn't try to force that shape. Instead, on every finalize, `_receiver`
notes the wall-clock moment and logs the gap to the *next* audio chunk
received afterward -- labeled explicitly as a best-effort, not a hard
sync point, since chunk boundaries aren't guaranteed to line up with the
text-finalization heuristic (a chunk logged right after a finalize may
still be trailing audio for the *previous* phrase, still streaming out).
Never compare this number directly to a Step 7 breakdown in logs or docs.

A small implementation note: `_safe_send`/`_pcm16_to_wav_bytes` below are
intentionally near-duplicates of the same-named helpers in handlers.py
rather than imports from there -- handlers.py imports `run_live_session`
from this module, so importing back from handlers.py would be a circular
import. They're both under 10 lines; duplicating them here was judged
simpler and lower-risk than extracting a new shared-utility module for
this one delivery.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from backend.models.schemas import (
    AudioMessage,
    ErrorMessage,
    SetMutedMessage,
    StatusMessage,
    StopMessage,
    TranscriptMessage,
    TranslationMessage,
    parse_client_message,
)
from config.settings import Settings

logger = logging.getLogger(__name__)

# Gemini Live's native audio output rate, per Google's speech-generation
# docs (the same rate gemini_provider.py's separate TTS models use) --
# not independently confirmed against gemini-3.5-live-translate-preview
# specifically, since that needs a real API key to observe (see the
# module docstring). If translated audio plays back pitched/sped wrong,
# this is the first thing to check against what your own logs show.
_OUTPUT_SAMPLE_RATE = 24000


class _FatalLiveError(Exception):
    """Raised internally to unwind run_live_session's TaskGroup when the
    Live connection fails for a reason no amount of reconnecting will fix
    (see "Reconnection" in the module docstring). Already reported to the
    client via an ErrorMessage before being raised -- run_live_session's
    handler for it is a no-op."""


@dataclass
class _SharedState:
    """The only state `_sender` and `_receiver` share. `session` is the
    currently-connected Gemini Live session, or None whenever `_receiver`
    is between connections (at startup, mid-reconnect, or after a fatal
    error) -- `_sender` must check this before every send, since it never
    knows or waits for reconnects itself."""

    muted: bool = False
    stop_requested: bool = False
    session: Optional[object] = None  # google.genai.live.AsyncSession, kept untyped to avoid a module-level SDK import


@dataclass
class _LivePhrase:
    """Accumulates one discrete phrase's worth of input/output transcript
    text between finalize triggers -- see "Turning a continuous stream
    into discrete phrases" in the module docstring. Reset (a fresh
    instance) every time a finalize happens, whether or not anything was
    actually sent (an empty phrase finalizing is a harmless no-op -- see
    `_receiver`'s `finalize` closure)."""

    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    input_text: str = ""
    output_text: str = ""
    last_input_activity: float = field(default_factory=time.monotonic)
    opened_at: float = field(default_factory=time.monotonic)

    def is_empty(self) -> bool:
        return not self.input_text and not self.output_text


async def _safe_send(websocket: WebSocket, payload: str) -> bool:
    """Near-duplicate of handlers.py's helper of the same name -- see the
    module docstring's note on why this isn't imported instead."""
    try:
        await websocket.send_text(payload)
        return True
    except (RuntimeError, WebSocketDisconnect) as exc:
        logger.debug("Dropped a message because the client is gone: %s", exc)
        return False


def _pcm16_to_wav_bytes(pcm16_bytes: bytes, sample_rate: int, channels: int = 1) -> bytes:
    """Near-duplicate of handlers.py's helper of the same name -- see the
    module docstring's note on why this isn't imported instead."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)  # PCM16
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16_bytes)
    return buf.getvalue()


async def _close_quietly(session: Optional[object]) -> None:
    if session is None:
        return
    try:
        await session.close()  # type: ignore[attr-defined]
    except Exception:
        logger.debug("Error closing a Gemini Live session (already gone is fine)", exc_info=True)


async def _sender(websocket: WebSocket, shared: _SharedState, sample_rate: int) -> None:
    """The single, continuous reader of `websocket` for the whole
    start-to-stop cycle -- see the module docstring for why this survives
    across any number of `_receiver` reconnects rather than being
    recreated per-connection. Also the only place reading `stop`/
    `set_muted` control messages for this mode; a `start`/`audio_played`
    message arriving mid-Live-session is ignored (neither is meaningful
    here) rather than treated as an error."""
    from google.genai import types

    while True:
        message = await websocket.receive()

        if message.get("type") == "websocket.disconnect":
            shared.stop_requested = True
            await _close_quietly(shared.session)
            return

        if "bytes" in message and message["bytes"] is not None:
            if shared.muted or shared.session is None:
                # Muted: never forward audio into a billed session at all
                # (see "Mute" in the module docstring). No live session
                # right now (startup/reconnecting): drop rather than
                # buffer -- this mode has no utterance boundaries to
                # buffer *to*, so there's no good place to hold audio
                # until a session is ready without reinventing VAD.
                continue
            try:
                await shared.session.send_realtime_input(  # type: ignore[attr-defined]
                    audio=types.Blob(data=message["bytes"], mime_type=f"audio/pcm;rate={sample_rate}")
                )
            except Exception:
                logger.debug(
                    "Dropped an audio frame -- the Gemini Live session is mid-reconnect or just closed",
                    exc_info=True,
                )
            continue

        if "text" in message and message["text"] is not None:
            try:
                parsed = parse_client_message(message["text"])
            except ValidationError as exc:
                await _safe_send(websocket, ErrorMessage(message=f"Invalid message: {exc}").model_dump_json())
                continue

            if isinstance(parsed, StopMessage):
                shared.stop_requested = True
                await _close_quietly(shared.session)
                return
            if isinstance(parsed, SetMutedMessage):
                shared.muted = parsed.muted
                logger.info("Gemini Live: audio forwarding %s", "muted" if shared.muted else "unmuted")


async def _receiver(
    websocket: WebSocket,
    settings: Settings,
    source_lang: str,
    target_lang: str,
    shared: _SharedState,
) -> None:
    """Owns every Gemini Live connection attempt for this session: opens
    one, drives it (turning its response stream into finalized
    transcript/translation/audio messages -- see the module docstring)
    until it dies or a clean stop is requested, force-finalizes whatever
    phrase was in flight, and -- for anything short of a fatal access
    error -- reconnects and keeps going. The only writer to `websocket`
    for the life of this call."""
    from google import genai
    from google.genai import errors as genai_errors
    from google.genai import types

    client = genai.Client(api_key=settings.gemini_api_key)
    live_config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        # language_codes is a *hint* for auto-detection, not a hard source
        # language -- see "Source language" in the module docstring.
        input_audio_transcription=types.AudioTranscriptionConfig(language_codes=[source_lang]),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        translation_config=types.TranslationConfig(target_language_code=target_lang, echo_target_language=False),
    )

    await _safe_send(
        websocket,
        StatusMessage(
            status="listening",
            detail=(
                "This mode auto-detects the spoken language -- your source-language "
                "selection is used only as a hint, not enforced."
            ),
        ).model_dump_json(),
    )

    phrase = _LivePhrase()
    pending_finalize_at: Optional[float] = None  # see "Latency" in the module docstring
    finalize_silence_s = settings.gemini_live_finalize_silence_ms / 1000
    max_phrase_s = settings.gemini_live_max_phrase_seconds
    attempt = 0

    async def finalize(reason: str) -> None:
        nonlocal phrase, pending_finalize_at
        if not phrase.is_empty():
            input_text = phrase.input_text.strip()
            output_text = phrase.output_text.strip()
            await _safe_send(
                websocket,
                TranscriptMessage(text=input_text, is_final=True, timestamp=phrase.started_at).model_dump_json(),
            )
            if output_text:
                await _safe_send(
                    websocket,
                    TranslationMessage(
                        text=output_text,
                        is_final=True,
                        source_lang=source_lang,
                        target_lang=target_lang,
                        timestamp=phrase.started_at,
                    ).model_dump_json(),
                )
            logger.info("Gemini Live: finalized a phrase (%s) -- %r -> %r", reason, input_text, output_text)
            pending_finalize_at = time.monotonic()
        phrase = _LivePhrase()

    while not shared.stop_requested:
        attempt += 1
        try:
            async with client.aio.live.connect(model=settings.gemini_live_model, config=live_config) as session:
                if shared.stop_requested:
                    return
                shared.session = session
                # `session.receive()` is NOT one continuous stream for the
                # whole connection -- reading the installed SDK's own
                # source (google.genai.live.AsyncSession.receive) shows
                # its internal loop yields messages only up to and
                # including the first one with server_content.turn_complete
                # True, then the generator ends on its own. So staying
                # connected across multiple turns means calling receive()
                # again each time it ends this way -- this outer loop is
                # that re-arm, not a reconnect (the underlying connection/
                # `session` is unchanged). It only exits via `stop_requested`
                # or an exception (a real connection failure), handled below.
                while not shared.stop_requested:
                    async for response in session.receive():
                        if shared.stop_requested:
                            return

                        content = response.server_content
                        if content is None:
                            continue

                        if content.input_transcription is not None and content.input_transcription.text:
                            phrase.input_text += content.input_transcription.text
                            phrase.last_input_activity = time.monotonic()
                        if content.output_transcription is not None and content.output_transcription.text:
                            phrase.output_text += content.output_transcription.text

                        if content.model_turn is not None:
                            for part in content.model_turn.parts or []:
                                inline = part.inline_data
                                if inline is None or not inline.data:
                                    continue
                                if pending_finalize_at is not None:
                                    logger.info(
                                        "Gemini Live: finalize -> next audio chunk gap %.0fms "
                                        "(best-effort pairing, see 'Latency' in the module docstring)",
                                        (time.monotonic() - pending_finalize_at) * 1000,
                                    )
                                    pending_finalize_at = None
                                await _safe_send(
                                    websocket,
                                    AudioMessage(
                                        audio_base64=base64.b64encode(
                                            _pcm16_to_wav_bytes(inline.data, _OUTPUT_SAMPLE_RATE)
                                        ).decode("ascii"),
                                        sample_rate=_OUTPUT_SAMPLE_RATE,
                                        timestamp=phrase.started_at,
                                    ).model_dump_json(),
                                )

                        # turn_complete is not just "a plausible field to
                        # check defensively" -- per the SDK source noted
                        # above, it's guaranteed to be True on the last
                        # message of every receive() call, so this branch
                        # reliably fires at least once per turn even if
                        # nothing else here ever does.
                        input_finished = bool(content.input_transcription and content.input_transcription.finished)
                        turn_done = bool(content.turn_complete or content.generation_complete)
                        silence_elapsed = not phrase.is_empty() and (
                            time.monotonic() - phrase.last_input_activity
                        ) >= finalize_silence_s
                        over_cap = (time.monotonic() - phrase.opened_at) >= max_phrase_s

                        if input_finished:
                            await finalize("native input_transcription.finished")
                        elif turn_done:
                            await finalize("native turn_complete/generation_complete")
                        elif silence_elapsed:
                            await finalize("input-silence timeout")
                        elif over_cap:
                            await finalize("max-phrase-duration safety cap")

        except asyncio.CancelledError:
            raise
        except genai_errors.ClientError as exc:
            if exc.code in (401, 403, 404):
                await finalize("session ended -- fatal access error")
                await _safe_send(
                    websocket,
                    ErrorMessage(
                        message=(
                            f"Gemini Live access error ({exc.code}): {exc}. Check GEMINI_API_KEY and that "
                            f"this key has access to {settings.gemini_live_model} -- it's a preview model, "
                            "which may be gated separately from the main Gemini API."
                        )
                    ).model_dump_json(),
                )
                shared.stop_requested = True
                raise _FatalLiveError(str(exc)) from exc
            logger.warning("Gemini Live request error on attempt #%d (retrying): %s", attempt, exc)
        except Exception:
            if shared.stop_requested:
                # Expected: _sender's response to `stop`/disconnect is to
                # close the current session (see _close_quietly), which
                # is exactly what just unblocked session.receive() here --
                # not a real failure, so don't log it as one.
                logger.debug("Gemini Live session closed as part of a clean stop", exc_info=True)
            else:
                logger.exception("Gemini Live session ended unexpectedly on attempt #%d (retrying)", attempt)
        finally:
            shared.session = None

        if shared.stop_requested:
            break
        await finalize("reconnecting")
        backoff_s = min(attempt * 1.0, 5.0)
        logger.info("Gemini Live: reconnecting in %.0fs (attempt #%d)", backoff_s, attempt + 1)
        await asyncio.sleep(backoff_s)

    await finalize("session stopped")


async def run_live_session(
    websocket: WebSocket,
    settings: Settings,
    source_lang: str,
    target_lang: str,
    sample_rate: int,
) -> None:
    """Entry point called from handlers.py once a `start` message has
    already been validated there (see the dispatch note in that module).
    Runs the entire start-to-stop lifecycle for Gemini Live Translate mode
    -- see the module docstring for the two-task design. Returns once the
    client sends `stop`, disconnects, or a fatal error ends the session;
    control then goes back to handle_connection's outer loop, same as
    every other provider's `start`...`stop` cycle."""
    shared = _SharedState()
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(_sender(websocket, shared, sample_rate), name="gemini_live_sender")
            tg.create_task(
                _receiver(websocket, settings, source_lang, target_lang, shared), name="gemini_live_receiver"
            )
    except* _FatalLiveError:
        # Already reported to the client (ErrorMessage) inside _receiver
        # before it raised this -- nothing more to do.
        pass
    except* Exception as eg:
        for exc in eg.exceptions:
            logger.exception("Unhandled error in a Gemini Live session task", exc_info=exc)
        await _safe_send(
            websocket, ErrorMessage(message="The live translation session failed unexpectedly").model_dump_json()
        )

    await _safe_send(websocket, StatusMessage(status="connected").model_dump_json())

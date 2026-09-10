"""
Scratch verification script for Phase 11 (meeting broadcast mode) -- NOT
part of the delivered app. Same mocked/synthetic pattern as
_verify_conversation_mode.py / _verify_live_handlers.py: fake only the
provider and the WebSocket transport, drive everything else
(handle_meeting_ingest, handle_meeting_listener, the real SpeechSegmenter,
the real MeetingRegistry, the real asyncio.Queue consumer) for real. Run
with: python3 _verify_meeting_handlers.py

Covers the plan's "Verification" section for this phase: exactly-two-
other-languages translation, identical broadcasts within a room, no
leakage across rooms, pass-through-only for same-language listeners,
provider call counts staying flat regardless of listener count,
unrecognized-language drop, and rejecting a second concurrent ingest for
the same meeting.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import struct
import sys
import traceback
from typing import AsyncIterator, List, Optional, Tuple

sys.path.insert(0, ".")

from backend.translation.base import Transcription, TranslationEvent, TranslationProvider
from backend.websocket import meeting_handlers
from backend.websocket.meeting_registry import MeetingRegistry
from config.settings import Settings


# --- Fake WebSocket (same shape as the other _verify_*.py scripts, plus a
# no-op close() since meeting_handlers.py calls websocket.close() on the
# reject paths) -----------------------------------------------------------
class FakeWebSocket:
    def __init__(self):
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.closed = False

    def queue_text(self, payload: dict) -> None:
        self.inbox.put_nowait({"type": "websocket.receive", "text": json.dumps(payload)})

    def queue_bytes(self, data: bytes) -> None:
        self.inbox.put_nowait({"type": "websocket.receive", "bytes": data})

    def queue_disconnect(self) -> None:
        self.inbox.put_nowait({"type": "websocket.disconnect"})

    async def receive(self) -> dict:
        return await self.inbox.get()

    async def send_text(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    async def close(self, code: int = 1000) -> None:
        self.closed = True

    def sent_of_type(self, type_: str) -> list[dict]:
        return [m for m in self.sent if m.get("type") == type_]


async def wait_until(predicate, timeout=5.0, interval=0.005):
    elapsed = 0.0
    while elapsed < timeout:
        if predicate():
            return True
        await asyncio.sleep(interval)
        elapsed += interval
    return predicate()


async def settle(seconds=0.15):
    await asyncio.sleep(seconds)


def _tone_frame(duration_s: float, sample_rate: int = 16000) -> bytes:
    n = int(sample_rate * duration_s)
    samples = [int(0.2 * 32767 * math.sin(2 * math.pi * 440.0 * i / sample_rate)) for i in range(n)]
    return struct.pack(f"<{n}h", *samples)


def _silence_frame(duration_s: float, sample_rate: int = 16000) -> bytes:
    n = int(sample_rate * duration_s)
    return b"\x00\x00" * n


def speak_one_phrase(ws: FakeWebSocket) -> None:
    """One phrase: a couple of 100ms speech frames, then enough silence
    (vad_end_silence_ms=250 in settings_for_test -> 3x100ms) to finalize."""
    ws.queue_bytes(_tone_frame(0.1))
    ws.queue_bytes(_tone_frame(0.1))
    for _ in range(3):
        ws.queue_bytes(_silence_frame(0.1))


class CountingProvider(TranslationProvider):
    """Fake provider that records exactly how many times each Phase 11
    hook was called and with what args -- the "bounded call count
    regardless of listener count" property is the whole point of this
    architecture, so it needs to be verifiable precisely, not just
    plausibly (mock_provider.py's placeholder text is good enough for
    manual testing but doesn't expose call args the way this needs)."""

    def __init__(self, transcript_text: str = "hello there") -> None:
        self.detected_language: Optional[str] = "en"
        self.transcript_text = transcript_text
        self.transcribe_final_calls = 0
        self.translate_final_calls: List[Tuple[str, str, str]] = []  # (text, source_lang, target_lang)
        self.synthesize_calls: List[str] = []

    async def start_session(self, source_lang: str, target_lang: str) -> None:
        pass

    async def process_audio_chunk(self, pcm16_bytes: bytes) -> list[TranslationEvent]:
        return []

    async def transcribe_final(self, pcm16_bytes: bytes) -> Optional[Transcription]:
        self.transcribe_final_calls += 1
        return Transcription(text=self.transcript_text, detected_language=self.detected_language)

    async def translate_final(self, text: str, source_lang: str, target_lang: str) -> Optional[str]:
        self.translate_final_calls.append((text, source_lang, target_lang))
        return f"[{target_lang}] {text}"

    async def synthesize_speech(self, text: str) -> AsyncIterator[tuple[bytes, int]]:
        self.synthesize_calls.append(text)
        yield b"\x01\x02\x03\x04", 16000

    async def close_session(self) -> list[TranslationEvent]:
        return []


def settings_for_test(**overrides) -> Settings:
    kwargs = dict(
        translation_provider="mock",  # irrelevant -- get_provider is monkeypatched
        vad_threshold=0.01,
        vad_pre_speech_ms=0.0,
        vad_end_silence_ms=250.0,
        vad_partial_interval_ms=100.0,
        meeting_languages=["en", "az", "ru"],
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


results = []
_real_get_provider = meeting_handlers.get_provider


def record(name, status, detail=""):
    results.append((name, status, detail))
    print(f"[{status}] {name}" + (f" -- {detail}" if detail and status == "FAIL" else ""))


async def start_ingest(registry, settings, provider, meeting_id="m1"):
    meeting_handlers.get_provider = lambda s: provider
    ws = FakeWebSocket()
    task = asyncio.create_task(meeting_handlers.handle_meeting_ingest(ws, settings, registry, meeting_id))
    ws.queue_text({"type": "start", "sample_rate": 16000})
    await wait_until(lambda: len(ws.sent_of_type("status")) >= 1)
    return ws, task


async def stop_ingest(ws, task):
    ws.queue_text({"type": "stop"})
    ws.queue_disconnect()
    await asyncio.wait_for(task, timeout=5.0)
    meeting_handlers.get_provider = _real_get_provider


async def start_listener(registry, settings, meeting_id, lang):
    ws = FakeWebSocket()
    task = asyncio.create_task(meeting_handlers.handle_meeting_listener(ws, settings, registry, meeting_id, lang))
    await wait_until(lambda: len(ws.sent_of_type("status")) >= 1)
    return ws, task


async def stop_listener(ws, task):
    ws.queue_disconnect()
    await asyncio.wait_for(task, timeout=5.0)


async def test_translates_into_exactly_two_other_languages():
    registry = MeetingRegistry()
    settings = settings_for_test()
    provider = CountingProvider()
    provider.detected_language = "en"

    az_ws, az_task = await start_listener(registry, settings, "m1", "az")
    ru_ws, ru_task = await start_listener(registry, settings, "m1", "ru")
    ingest_ws, ingest_task = await start_ingest(registry, settings, provider)

    speak_one_phrase(ingest_ws)
    await wait_until(lambda: len(provider.translate_final_calls) >= 2)
    await settle()

    await stop_ingest(ingest_ws, ingest_task)
    await stop_listener(az_ws, az_task)
    await stop_listener(ru_ws, ru_task)

    assert provider.transcribe_final_calls == 1, provider.transcribe_final_calls
    targets = {t for (_, _, t) in provider.translate_final_calls}
    assert targets == {"az", "ru"}, targets
    assert all(s == "en" for (_, s, _) in provider.translate_final_calls), provider.translate_final_calls
    assert len(provider.synthesize_calls) == 2, provider.synthesize_calls
    record("translates into exactly the two other languages, never the source", "PASS")


async def test_same_room_listeners_get_identical_broadcasts():
    registry = MeetingRegistry()
    settings = settings_for_test()
    provider = CountingProvider()
    provider.detected_language = "en"

    listeners = [await start_listener(registry, settings, "m1", "az") for _ in range(3)]
    ingest_ws, ingest_task = await start_ingest(registry, settings, provider)

    speak_one_phrase(ingest_ws)
    await wait_until(lambda: all(len(ws.sent_of_type("translation")) >= 1 for ws, _ in listeners))
    await settle()

    await stop_ingest(ingest_ws, ingest_task)
    for ws, task in listeners:
        await stop_listener(ws, task)

    payloads = [[m for m in ws.sent if m["type"] in ("translation", "audio")] for ws, _ in listeners]
    assert payloads[0] == payloads[1] == payloads[2], payloads
    assert len(payloads[0]) >= 1
    record("listeners in the same room receive byte-identical broadcasts", "PASS")


async def test_rooms_never_leak_across_languages():
    registry = MeetingRegistry()
    settings = settings_for_test()
    provider = CountingProvider()
    provider.detected_language = "en"

    en_ws, en_task = await start_listener(registry, settings, "m1", "en")
    az_ws, az_task = await start_listener(registry, settings, "m1", "az")
    ru_ws, ru_task = await start_listener(registry, settings, "m1", "ru")
    ingest_ws, ingest_task = await start_ingest(registry, settings, provider)

    speak_one_phrase(ingest_ws)
    await wait_until(lambda: len(az_ws.sent_of_type("translation")) >= 1 and len(ru_ws.sent_of_type("translation")) >= 1)
    await settle()

    await stop_ingest(ingest_ws, ingest_task)
    await stop_listener(en_ws, en_task)
    await stop_listener(az_ws, az_task)
    await stop_listener(ru_ws, ru_task)

    assert len(en_ws.sent_of_type("transcript")) == 1, en_ws.sent
    assert en_ws.sent_of_type("translation") == [] and en_ws.sent_of_type("audio") == [], en_ws.sent

    for ws, lang in ((az_ws, "az"), (ru_ws, "ru")):
        assert ws.sent_of_type("transcript") == [], (lang, ws.sent)
        translations = ws.sent_of_type("translation")
        assert len(translations) == 1 and translations[0]["target_lang"] == lang, (lang, translations)
        assert len(ws.sent_of_type("audio")) >= 1, (lang, ws.sent)

    record("rooms never leak across languages -- each listener sees only its own room", "PASS")


async def test_same_language_listener_gets_pass_through_transcript_only():
    registry = MeetingRegistry()
    settings = settings_for_test()
    provider = CountingProvider()
    provider.detected_language = "en"

    en_ws, en_task = await start_listener(registry, settings, "m1", "en")
    ingest_ws, ingest_task = await start_ingest(registry, settings, provider)

    speak_one_phrase(ingest_ws)
    await wait_until(lambda: len(en_ws.sent_of_type("transcript")) >= 1)
    await settle()

    await stop_ingest(ingest_ws, ingest_task)
    await stop_listener(en_ws, en_task)

    transcripts = en_ws.sent_of_type("transcript")
    assert len(transcripts) == 1 and transcripts[0]["text"] == provider.transcript_text, transcripts
    assert en_ws.sent_of_type("translation") == [] and en_ws.sent_of_type("audio") == [], en_ws.sent
    record("same-language listener gets a pass-through transcript only, no wasted synth", "PASS")


async def test_provider_calls_bounded_regardless_of_listener_count():
    for n_listeners in (0, 1, 20):
        registry = MeetingRegistry()
        settings = settings_for_test()
        provider = CountingProvider()
        provider.detected_language = "en"

        listeners = []
        for i in range(n_listeners):
            lang = ["en", "az", "ru"][i % 3]
            listeners.append(await start_listener(registry, settings, "m1", lang))

        ingest_ws, ingest_task = await start_ingest(registry, settings, provider)
        speak_one_phrase(ingest_ws)
        await wait_until(lambda: len(provider.synthesize_calls) >= 2)
        await settle()

        await stop_ingest(ingest_ws, ingest_task)
        for ws, task in listeners:
            await stop_listener(ws, task)

        assert provider.transcribe_final_calls == 1, (n_listeners, provider.transcribe_final_calls)
        assert len(provider.translate_final_calls) == 2, (n_listeners, provider.translate_final_calls)
        assert len(provider.synthesize_calls) == 2, (n_listeners, provider.synthesize_calls)

    record("provider call counts stay bounded (1/2/2) regardless of 0/1/20 listeners", "PASS")


async def test_unrecognized_language_drops_the_utterance():
    registry = MeetingRegistry()
    settings = settings_for_test()
    provider = CountingProvider()
    provider.detected_language = "fr"  # not in settings.meeting_languages

    en_ws, en_task = await start_listener(registry, settings, "m1", "en")
    ingest_ws, ingest_task = await start_ingest(registry, settings, provider)

    speak_one_phrase(ingest_ws)
    await wait_until(lambda: provider.transcribe_final_calls >= 1)
    await settle()

    await stop_ingest(ingest_ws, ingest_task)
    await stop_listener(en_ws, en_task)

    assert provider.translate_final_calls == [], provider.translate_final_calls
    assert provider.synthesize_calls == [], provider.synthesize_calls
    assert en_ws.sent_of_type("transcript") == [], en_ws.sent
    assert en_ws.sent_of_type("translation") == [] and en_ws.sent_of_type("audio") == [], en_ws.sent
    record("an utterance in an unrecognized/undetected language is dropped cleanly", "PASS")


async def test_second_ingest_for_same_meeting_is_rejected():
    registry = MeetingRegistry()
    settings = settings_for_test()
    provider_a = CountingProvider()
    provider_a.detected_language = "en"

    ingest_ws_a, ingest_task_a = await start_ingest(registry, settings, provider_a, meeting_id="m1")

    meeting_handlers.get_provider = lambda s: CountingProvider()
    ingest_ws_b = FakeWebSocket()
    task_b = asyncio.create_task(meeting_handlers.handle_meeting_ingest(ingest_ws_b, settings, registry, "m1"))
    await asyncio.wait_for(task_b, timeout=5.0)
    meeting_handlers.get_provider = _real_get_provider

    errors_b = ingest_ws_b.sent_of_type("error")
    assert len(errors_b) == 1 and "already has an active ingest" in errors_b[0]["message"], errors_b
    assert ingest_ws_b.closed is True

    # First session must be entirely unaffected -- confirm it still works.
    az_ws, az_task = await start_listener(registry, settings, "m1", "az")
    speak_one_phrase(ingest_ws_a)
    await wait_until(lambda: len(az_ws.sent_of_type("translation")) >= 1)
    await settle()

    await stop_ingest(ingest_ws_a, ingest_task_a)
    await stop_listener(az_ws, az_task)

    assert provider_a.transcribe_final_calls == 1, provider_a.transcribe_final_calls
    record("a second ingest for the same meeting is rejected; the first is unaffected", "PASS")


async def main():
    logging.basicConfig(level=logging.CRITICAL)
    tests = [
        test_translates_into_exactly_two_other_languages,
        test_same_room_listeners_get_identical_broadcasts,
        test_rooms_never_leak_across_languages,
        test_same_language_listener_gets_pass_through_transcript_only,
        test_provider_calls_bounded_regardless_of_listener_count,
        test_unrecognized_language_drops_the_utterance,
        test_second_ingest_for_same_meeting_is_rejected,
    ]
    failed = 0
    for t in tests:
        try:
            await t()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            record(t.__name__, "FAIL", f"{exc}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} tests passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())

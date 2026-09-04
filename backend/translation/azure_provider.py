"""
Azure Speech Translation provider.

Uses the Azure Speech SDK's purpose-built `TranslationRecognizer`, which
does speech-to-text and translation in a single streaming call -- a
different shape from Gemini's non-streaming `interactions.create` (see
gemini_provider.py) and one Microsoft's docs suggest is actually built for
low-latency interactive use, unlike the "experimental" Interactions API.
This is the whole reason to try it: see the Step 7 latency investigation
in the project chat -- an isolated Gemini call was taking ~20s for STT+
translation alone, and this provider exists to find out whether Azure's
purpose-built endpoint does meaningfully better.

Design: one fresh `TranslationRecognizer` (over a fresh `PushAudioInputStream`)
per utterance, calling `recognize_once_async()`, rather than one persistent
recognizer doing continuous recognition for the whole session. This keeps
the per-utterance shape identical to gemini_provider.py/local_provider.py
(hand the provider one complete utterance, get one atomic result back), so
Step 7's latency breakdowns stay apples-to-apples across providers, and it
avoids having to reconcile our own VAD-drawn phrase boundaries with Azure's
own separate endpointing inside a long-lived stream. The trade-off: each
utterance pays its own connection/auth setup cost instead of reusing one
already-open connection -- if the latency breakdown shows that setup cost
is a meaningful chunk of the total, switching to one persistent
continuous-recognition connection per session is the next thing to try.

Text-to-speech (Step 7 optimization): the first version of this provider
made a second, separate `SpeechSynthesizer` call on the already-translated
text -- simpler to reason about, but a real isolated test showed it costing
~2.5s of its own on top of the ~2.8s recognition+translation call. Azure's
`TranslationRecognizer` can produce that same audio for free as a side
effect of the *same* call, via its `synthesizing` event (see
how-to-translate-speech in Microsoft's docs) -- one API round trip instead
of two. The trade-off: it only works for a single target language (true
for every language pair this app uses) and the audio it hands back is
chunked by Azure's own internal buffering, not by our `split_for_speech`
sentence/clause boundaries -- doesn't matter here since `synthesize_speech`
just replays whatever chunks arrived, in order.

`process_audio_chunk` stashes whatever audio came back (possibly none) in
`self._pending_audio`; `synthesize_speech` -- called right after, for the
same phrase, by the same single-consumer loop in handlers.py -- drains and
replays it instead of making its own request. `text` is unused there: the
audio already reflects that exact translation. One consequence worth
knowing about when reading a Step 7 latency breakdown for this provider:
"Voice generation (TTS)" will now legitimately read close to 0ms, because
the real synthesis time happened *inside* the "Speech recognition" leg
(the one atomic call that produced transcript + translation + audio
together) -- the same reason an atomic provider's "Translation" leg
legitimately reads ~0ms (see TranslationEvent.generated_at below). And
since there's no way to ask the provider not to bother synthesizing (the
`TranslationProvider` interface only signals mute by *not calling*
`synthesize_speech` at all, after the fact -- see base.py), a muted phrase
still pays for synthesis on Azure's side; the audio is just computed and
then discarded (`process_audio_chunk` clears any unconsumed
`_pending_audio` from the previous phrase before starting a new one, so a
skipped phrase's audio can never bleed into the next one's playback).

Requires: settings.azure_speech_key / azure_speech_region (see
config/settings.py, AZURE_SPEECH_KEY / AZURE_SPEECH_REGION in .env --
AZURE_SPEECH_REGION is the short region code from your resource's STT
endpoint, e.g. "eastus2" out of "https://eastus2.stt.speech.microsoft.com").
Select this provider by setting TRANSLATION_PROVIDER=azure.

Language coverage caveat: Azure's speech-translation *target* list (which
this app relies on for az/ru as translation output) is confirmed to
include Azerbaijani and Russian, each with a Neural TTS voice (az-AZ-
BanuNeural/BabekNeural; ru-RU-SvetlanaNeural) -- see the docs linked above.
Using Azerbaijani or Russian as the *source* (spoken) language specifically
within the translation recognizer (as opposed to plain speech-to-text,
which does list az-AZ/ru-RU locales) wasn't separately confirmed -- if
`source_lang="az"` or `"ru"` throws or silently fails to recognize, that's
the likely reason; en -> az / en -> ru (this app's default direction)
doesn't hit that edge case.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from backend.translation.base import EventKind, TranslationEvent, TranslationProvider

logger = logging.getLogger(__name__)

# Our internal language codes (config/languages.py) -> Azure locale codes.
# Speech *translation* target languages are the bare codes already (see
# add_target_language below) -- only the recognition *source* needs a full
# region-qualified locale.
_LOCALE_BY_LANG = {
    "en": "en-US",
    "az": "az-AZ",
    "ru": "ru-RU",
}

# One reasonable default Neural voice per language we support, requested
# as part of the same recognize_once call (see _recognize_once). Azure's
# voice list moves over time -- if a voice name here starts erroring, check
# https://speech.microsoft.com/portal/voicegallery (or the language-support
# docs) for the current name and swap it in.
_VOICE_BY_LANG = {
    "en": "en-US-JennyNeural",
    "az": "az-AZ-BanuNeural",
    "ru": "ru-RU-SvetlanaNeural",
}

# Matches the Raw16Khz16BitMonoPcm format requested in _recognize_once --
# raw PCM, not WAV, so handlers.py's existing WAV-wrapping
# (_pcm16_to_wav_bytes) stays in charge, same as every other provider.
_TTS_SAMPLE_RATE = 16000


class AzureSpeechTranslationProvider(TranslationProvider):
    def __init__(self, speech_key: str | None, region: str | None, sample_rate: int = 16000) -> None:
        if not speech_key or not region:
            raise ValueError(
                "AZURE_SPEECH_KEY and AZURE_SPEECH_REGION are required to use the "
                "azure translation provider"
            )
        self._speech_key = speech_key
        self._region = region
        self._sample_rate = sample_rate
        self._source_lang = "en"
        self._target_lang = "az"
        # Step 7 optimization: audio captured by the *previous*
        # process_audio_chunk call's bundled synthesis, waiting to be
        # replayed by the very next synthesize_speech call -- see the
        # module docstring. Always a list of raw PCM16 chunks (possibly
        # empty); never carries over past one process_audio_chunk call.
        self._pending_audio: list[bytes] = []

    async def start_session(self, source_lang: str, target_lang: str) -> None:
        if source_lang not in _LOCALE_BY_LANG:
            raise ValueError(f"AzureSpeechTranslationProvider has no locale mapping for source_lang={source_lang!r}")
        self._source_lang = source_lang
        self._target_lang = target_lang
        self._pending_audio = []

    def _recognize_once(self, pcm16_bytes: bytes):
        """Blocking: runs the whole recognize-and-translate round trip for
        one utterance on a fresh recognizer/stream, *and* collects
        whatever audio Azure's bundled `synthesizing` event produces for
        the translation along the way (Step 7 optimization -- see the
        module docstring). Always called via run_in_executor -- the Speech
        SDK's Python bindings are synchronous/callback-based, not
        asyncio-native. Returns (result, speechsdk, audio_chunks)."""
        import azure.cognitiveservices.speech as speechsdk

        translation_config = speechsdk.translation.SpeechTranslationConfig(
            subscription=self._speech_key, region=self._region
        )
        translation_config.speech_recognition_language = _LOCALE_BY_LANG[self._source_lang]
        translation_config.add_target_language(self._target_lang)
        # Ask this same call to also synthesize the translation -- only
        # valid for a single target language, which is all this app ever
        # configures. Raw PCM, matching synthesize_speech's contract
        # (base.py) directly, no WAV-header stripping needed.
        translation_config.voice_name = _VOICE_BY_LANG.get(self._target_lang, _VOICE_BY_LANG["en"])
        translation_config.set_speech_synthesis_output_format(
            speechsdk.SpeechSynthesisOutputFormat.Raw16Khz16BitMonoPcm
        )

        audio_format = speechsdk.audio.AudioStreamFormat(
            samples_per_second=self._sample_rate, bits_per_sample=16, channels=1
        )
        push_stream = speechsdk.audio.PushAudioInputStream(stream_format=audio_format)
        audio_config = speechsdk.audio.AudioConfig(stream=push_stream)

        recognizer = speechsdk.translation.TranslationRecognizer(
            translation_config=translation_config, audio_config=audio_config
        )

        audio_chunks: list[bytes] = []

        def _on_synthesizing(evt) -> None:
            # A zero-length chunk signals "end of synthesis" -- see
            # Microsoft's samples -- not audio to keep.
            data = evt.result.audio
            if data:
                audio_chunks.append(data)

        recognizer.synthesizing.connect(_on_synthesizing)

        # Start listening before pushing bytes so the recognizer is already
        # attached to the stream when data arrives, then close the stream
        # immediately -- this is one complete, already-VAD-bounded
        # utterance, not a live mic feed, so there's nothing to trickle in.
        future = recognizer.recognize_once_async()
        push_stream.write(pcm16_bytes)
        push_stream.close()
        result = future.get()
        recognizer.synthesizing.disconnect_all()
        return result, speechsdk, audio_chunks

    async def process_audio_chunk(self, pcm16_bytes: bytes) -> list[TranslationEvent]:
        # Discard whatever the previous phrase's bundled synthesis
        # produced but nobody consumed (e.g. the client had muted
        # playback, so synthesize_speech was never called for it) --
        # otherwise it would get replayed against the wrong phrase.
        self._pending_audio = []

        if not pcm16_bytes:
            return []

        loop = asyncio.get_event_loop()
        try:
            result, speechsdk, audio_chunks = await loop.run_in_executor(None, self._recognize_once, pcm16_bytes)
        except Exception:
            logger.exception("Azure speech translation request failed for an utterance")
            return []

        if result.reason == speechsdk.ResultReason.TranslatedSpeech:
            transcript = result.text or ""
            translation = result.translations.get(self._target_lang, "") if result.translations else ""
            if not transcript and not translation:
                return []
            if not audio_chunks:
                logger.warning(
                    "Azure speech translation succeeded but its bundled synthesis produced no audio "
                    "for this utterance -- check that voice_name (%s) is a valid voice for target_lang=%r",
                    _VOICE_BY_LANG.get(self._target_lang, _VOICE_BY_LANG["en"]),
                    self._target_lang,
                )
            self._pending_audio = audio_chunks
            return [
                TranslationEvent(kind=EventKind.TRANSCRIPT, text=transcript, is_final=True),
                TranslationEvent(kind=EventKind.TRANSLATION, text=translation, is_final=True),
            ]

        if result.reason == speechsdk.ResultReason.NoMatch:
            # Nothing intelligible in this utterance -- not an error, just
            # nothing to report (same as the other providers on silence).
            return []

        if result.reason == speechsdk.ResultReason.Canceled:
            details = result.cancellation_details
            logger.warning(
                "Azure speech translation canceled: reason=%s error_details=%s",
                details.reason,
                details.error_details,
            )
            return []

        return []

    async def synthesize_speech(self, text: str) -> AsyncIterator[tuple[bytes, int]]:
        # `text` is unused -- see the module docstring. The audio was
        # already produced as a side effect of the process_audio_chunk
        # call for this same phrase; replay it instead of making a new
        # request.
        chunks, self._pending_audio = self._pending_audio, []
        for chunk in chunks:
            yield chunk, _TTS_SAMPLE_RATE

    async def close_session(self) -> list[TranslationEvent]:
        # Each utterance is a fully self-contained recognize_once call --
        # there's no persistent connection or buffered partial result to
        # flush here (contrast local_provider.py, which has none either,
        # for the same reason).
        return []

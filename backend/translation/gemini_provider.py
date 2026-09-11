"""
Google Gemini provider.

For each complete utterance, sends the audio straight to Gemini (native
audio understanding -- no separate speech-to-text step) with instructions
to transcribe it in the source language and translate that transcript into
the target language, requesting structured JSON output so both fields come
back reliably instead of having to parse free-form text. For in-progress
utterances (see transcribe_partial), a second, cheaper prompt asks for a
transcript only -- no translation -- since partial results are for live
feedback and never get translated (see base.py).

The translation instruction (Step 5) is tuned for natural, spoken meeting
language rather than a formal document translation, and explicitly told to
preserve numbers/dates/names/company names verbatim -- see
_transcribe_and_translate's prompt below and "Translation quality" in the
README.

Phase 8 (streaming translation) adds translate_partial: a third, cheaper
prompt (text-only -- no audio) that translates just a newly-stabilized
fragment of an in-progress transcript as a continuation of whatever's
already been committed for that phrase, rather than waiting for the whole
sentence -- see _translate_continuation below and "Using streaming
translation" in the README.

Step 6 adds speech synthesis of the translated text (synthesize_speech),
via a *different* API surface than everything above -- Gemini's native
text-to-speech models go through `client.models.generate_content` with
`response_modalities=["AUDIO"]`, not the Interactions API used for
transcription/translation. This hasn't been exercised against the live
API from inside this project's dev sandbox (no network access to Google
from there) -- see the note above _synthesize_chunk for what to check if
it errors on your machine.

Requires: settings.gemini_api_key (GEMINI_API_KEY in config/.env). Select
this provider by setting TRANSLATION_PROVIDER=gemini. You supply your own
key directly in config/.env -- this code never transmits it anywhere
except in authenticated requests to Google's own API, and config/.env is
git-ignored (see .gitignore) so it never gets committed.

Uses the "Interactions" API on the `google-genai` SDK
(https://github.com/googleapis/python-genai) -- `client.interactions.create`
-- Google's current unified interface, rather than the older
`generate_content` method.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import wave
from typing import AsyncIterator, Optional, Type, TypeVar

from pydantic import BaseModel

from backend.translation.base import EventKind, Transcription, TranslationEvent, TranslationProvider
from backend.translation.text_chunking import split_for_speech
from config.languages import SUPPORTED_LANGUAGES
from config.settings import get_settings

logger = logging.getLogger(__name__)

_LANGUAGE_NAMES = {lang["code"]: lang["name"] for lang in SUPPORTED_LANGUAGES}

_ResultT = TypeVar("_ResultT", bound=BaseModel)

# Gemini's native speech-generation models return raw PCM at a fixed
# 24kHz/16-bit/mono, regardless of the input audio's own sample rate --
# see https://ai.google.dev/gemini-api/docs/speech-generation. Unrelated
# to self._sample_rate (that's the *input* mic audio's rate, used for STT).
_TTS_SAMPLE_RATE = 24000


class _TranscriptionResult(BaseModel):
    transcript: str
    translation: str
    # ISO 639-1 code (e.g. "en"/"az"/"ru"), or null if Gemini couldn't tell.
    # Purely informational -- see TranslationEvent.detected_language in
    # base.py for why this never overrides the user's language selection.
    detected_language: Optional[str] = None


class _PartialTranscriptionResult(BaseModel):
    transcript: str


class _PartialTranslationResult(BaseModel):
    translation: str


class _AutoTranscriptionResult(BaseModel):
    """Phase 11 (meeting broadcast mode): transcribe_final's result shape --
    always auto-detects the spoken language (a meeting ingest stream has no
    single fixed source language), and deliberately has NO translation
    field -- unlike _TranscriptionResult above, meeting mode transcribes an
    utterance exactly once and translates it separately, per target
    language, via translate_final (see base.py for why)."""

    transcript: str
    detected_language: Optional[str] = None


class GeminiTranslationProvider(TranslationProvider):
    def __init__(
        self,
        api_key: Optional[str],
        model: str,
        sample_rate: int,
        tts_model: str = "gemini-2.5-flash-preview-tts",
        tts_voice: str = "Kore",
    ) -> None:
        if not api_key:
            raise ValueError(
                "GEMINI_API_KEY is required to use the gemini translation provider. "
                "Add it to config/.env -- see config/.env.example."
            )
        from google import genai
        from google.genai.interactions import AudioContent, TextContent

        self._client = genai.Client(api_key=api_key)
        # Kept as instance attrs so the request-building helpers below can
        # construct already-typed Content objects. This matters: the SDK's
        # `input` field is a lenient Union (Content | List[Step] |
        # List[Content] | str), and pydantic's smart-union matching
        # resolves a raw list of plain dicts against List[Step] first
        # (silently as "unknown step" variants) rather than List[Content]
        # -- so passing dicts like {"type": "text", ...} directly gets
        # swallowed into the wrong variant instead of raising, and the
        # request goes out empty in practice. Constructing
        # TextContent/AudioContent instances ourselves sidesteps the
        # ambiguity entirely. Verified against the installed google-genai
        # SDK's own pydantic models.
        self._TextContent = TextContent
        self._AudioContent = AudioContent
        self._model = model
        self._sample_rate = sample_rate
        self._tts_model = tts_model
        self._tts_voice = tts_voice
        self._source_lang = "en"
        self._target_lang = "az"

    async def start_session(self, source_lang: str, target_lang: str) -> None:
        self._source_lang = source_lang
        self._target_lang = target_lang

    async def process_audio_chunk(self, pcm16_bytes: bytes) -> list[TranslationEvent]:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, self._transcribe_and_translate, pcm16_bytes)
        if result is None:
            return []

        transcript = result.transcript.strip()
        translation = result.translation.strip()
        if not transcript:
            return []

        return [
            TranslationEvent(
                kind=EventKind.TRANSCRIPT,
                text=transcript,
                is_final=True,
                detected_language=result.detected_language,
            ),
            TranslationEvent(kind=EventKind.TRANSLATION, text=translation, is_final=True),
        ]

    async def transcribe_partial(self, pcm16_bytes: bytes) -> Optional[str]:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, self._transcribe_only, pcm16_bytes)
        if result is None:
            return None
        transcript = result.transcript.strip()
        return transcript or None

    async def translate_partial(self, new_stable_text: str, already_committed_translation: str) -> Optional[str]:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, self._translate_continuation, new_stable_text, already_committed_translation
        )
        if result is None:
            return None
        translation = result.translation.strip()
        return translation or None

    async def transcribe_final(self, pcm16_bytes: bytes) -> Optional[Transcription]:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, self._transcribe_auto, pcm16_bytes)
        if result is None:
            return None
        transcript = result.transcript.strip()
        if not transcript:
            return None
        return Transcription(text=transcript, detected_language=result.detected_language)

    async def translate_final(self, text: str, source_lang: str, target_lang: str) -> Optional[str]:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, self._translate_standalone, text, source_lang, target_lang)
        if result is None:
            return None
        translation = result.translation.strip()
        return translation or None

    async def synthesize_speech(self, text: str) -> AsyncIterator[tuple[bytes, int]]:
        loop = asyncio.get_event_loop()
        for chunk in split_for_speech(text):
            pcm = await loop.run_in_executor(None, self._synthesize_chunk, chunk)
            if pcm:
                yield pcm, _TTS_SAMPLE_RATE

    async def close_session(self) -> list[TranslationEvent]:
        return []

    # --- blocking helpers: always called via run_in_executor ---

    def _call_gemini(
        self, prompt: str, schema_model: Type[_ResultT], pcm16_bytes: Optional[bytes] = None
    ) -> Optional[_ResultT]:
        """`pcm16_bytes=None` builds a text-only request (Phase 8's
        translate_partial: the input is already-transcribed text, not
        audio, so there's nothing to attach -- a cheaper, faster round trip
        than every other call here, which directly helps the "start
        translating before the sentence ends" goal)."""
        content = [self._TextContent(type="text", text=prompt)]
        if pcm16_bytes is not None:
            content.append(
                self._AudioContent(
                    type="audio",
                    data=base64.b64encode(self._pcm16_to_wav(pcm16_bytes)).decode("ascii"),
                    mime_type="audio/wav",
                    # NOTE: do NOT also pass sample_rate/channels here -- the
                    # API rejects that combination ("Rate and channels are
                    # only supported for TYPE_L16 audio"). A WAV container
                    # already encodes its own sample rate and channel count
                    # in its header, so those fields would be redundant even
                    # if they were allowed.
                )
            )
        try:
            interaction = self._client.interactions.create(
                model=self._model,
                input=content,
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": schema_model.model_json_schema(),
                },
            )
        except Exception:
            logger.exception("Gemini interactions.create request failed")
            return None

        try:
            return schema_model.model_validate_json(interaction.output_text)
        except Exception:
            logger.exception("Could not parse Gemini response as the expected JSON shape: %r", interaction.output_text)
            return None

    def _transcribe_and_translate(self, pcm16_bytes: bytes) -> Optional[_TranscriptionResult]:
        target_name = _LANGUAGE_NAMES.get(self._target_lang, self._target_lang)

        # Phase 9: source_lang == "auto" (only ever set for this provider
        # via _AUTO_SOURCE_LANG_PROVIDERS in backend/websocket/handlers.py)
        # swaps the "spoken in X" framing for one that asks Gemini to
        # identify the language itself -- otherwise _LANGUAGE_NAMES.get(...)
        # falls through to the literal string "auto" and produces prompts
        # like "spoken in auto", which is wrong instruction, not just an
        # ugly one.
        if self._source_lang == "auto":
            source_clause = (
                "First identify the spoken language in the attached audio. Then "
                "transcribe exactly what is said, in that same language."
            )
        else:
            source_name = _LANGUAGE_NAMES.get(self._source_lang, self._source_lang)
            source_clause = (
                f"The attached audio is spoken in {source_name}. First transcribe "
                f"exactly what is said, in {source_name}."
            )

        # Step 5: the translation half of this prompt is deliberately tuned
        # for *spoken* output, not a formal document translation -- see
        # "Translation quality" in the README for the reasoning and how to
        # sanity-check it. Two things matter here: (1) natural/idiomatic
        # phrasing over a literal word-for-word rendering, since a live
        # meeting interpreter and a document translator produce
        # noticeably different output for the same sentence, and (2)
        # explicit instructions to preserve numbers/dates/names/company
        # names verbatim rather than risk the model "translating" or
        # mistranslating something that shouldn't change meaning at all.
        prompt = (
            f"{source_clause} Then translate that transcript into "
            f"{target_name}.\n\n"
            f"Translate the way a skilled human interpreter would in a live business "
            f"meeting, not the way a document translator would: natural, idiomatic, "
            f"spoken {target_name}, phrased the way a native {target_name} speaker "
            f"would actually say it out loud in conversation. Prefer the most natural "
            f"spoken phrasing over a literal, word-for-word rendering of the source "
            f"sentence structure -- reorder words, drop filler, or rephrase as needed "
            f"for that, the way an interpreter does, as long as the meaning stays the "
            f"same. Avoid stiff, overly formal, or bookish wording.\n\n"
            f"Keep numbers, dates, times, personal names, and company/product names "
            f"exactly as they refer to -- never translate, guess at, or alter what "
            f"they mean (only reformat them into {target_name}'s normal written "
            f"convention if that differs, e.g. date order or a decimal separator).\n\n"
            "Also report the spoken language you detected as an ISO 639-1 two-letter "
            "code (e.g. 'en', 'az', 'ru') in detected_language, or null if you can't "
            "tell. If the audio has no discernible speech (silence, noise, just "
            "breathing), return an empty string for both transcript and translation."
        )
        return self._call_gemini(prompt, _TranscriptionResult, pcm16_bytes=pcm16_bytes)

    def _transcribe_only(self, pcm16_bytes: bytes) -> Optional[_PartialTranscriptionResult]:
        # Deliberately cheaper than _transcribe_and_translate: no
        # translation is requested, since partial results are for live
        # transcript feedback only and are never translated (see
        # TranslationProvider.transcribe_partial in base.py) -- this call
        # may run several times per utterance as speech continues, so
        # keeping it minimal matters more here than for the one-shot final.
        if self._source_lang == "auto":
            source_clause = (
                "Identify the spoken language in the attached audio and transcribe "
                "exactly what is said so far, in that same language"
            )
        else:
            source_name = _LANGUAGE_NAMES.get(self._source_lang, self._source_lang)
            source_clause = (
                f"The attached audio is spoken in {source_name}. Transcribe exactly "
                f"what is said so far, in {source_name}"
            )
        prompt = (
            f"{source_clause} -- it may be a partial, unfinished "
            "sentence, that's expected. If there's no discernible speech yet, return "
            "an empty string."
        )
        return self._call_gemini(prompt, _PartialTranscriptionResult, pcm16_bytes=pcm16_bytes)

    def _translate_continuation(
        self, new_stable_text: str, already_committed_translation: str
    ) -> Optional[_PartialTranslationResult]:
        """Phase 8: translate a newly-stabilized fragment of the SOURCE
        transcript as a continuation of what's already been committed for
        this phrase -- text-only, no audio involved (the audio was already
        turned into text by transcribe_partial; re-sending it here would be
        redundant and slower). Deliberately told NOT to retranslate
        already_committed_translation, and that the source fragment may end
        mid-sentence -- both are expected/normal here, not errors."""
        target_name = _LANGUAGE_NAMES.get(self._target_lang, self._target_lang)

        if self._source_lang == "auto":
            source_intro = (
                f"You are live-translating a sentence into {target_name} as it's "
                f"being spoken, word by word, before the speaker has finished. "
                f"Identify the source language from the fragment itself."
            )
            fragment_label = "source-language"
        else:
            source_name = _LANGUAGE_NAMES.get(self._source_lang, self._source_lang)
            source_intro = (
                f"You are live-translating a sentence from {source_name} into "
                f"{target_name} as it's being spoken, word by word, before the "
                f"speaker has finished."
            )
            fragment_label = source_name

        if already_committed_translation:
            context_clause = (
                f"So far, this much has already been translated into {target_name} and "
                f"spoken aloud: \"{already_committed_translation}\". Do NOT repeat, "
                f"retranslate, or rephrase that part -- it's already done and already "
                f"spoken.\n\n"
            )
        else:
            context_clause = ""

        prompt = (
            f"{source_intro}\n\n"
            f"{context_clause}"
            f"Here is the NEXT new fragment of the {fragment_label} transcript, "
            f"which may end mid-sentence or mid-clause -- that's expected:\n"
            f"\"{new_stable_text}\"\n\n"
            f"Translate ONLY this new fragment, as a natural CONTINUATION of what's "
            f"already been translated (so the combined result reads as one coherent "
            f"sentence once both parts are joined), the way a live interpreter "
            f"continues speaking as new words arrive rather than waiting for the whole "
            f"sentence. Use the same natural, idiomatic, spoken register as a live "
            f"interpreter (not a formal document translation) -- see the guidance an "
            f"interpreter follows for the full sentence. Keep numbers, dates, times, "
            f"personal names, and company/product names exactly as they refer to. If "
            f"the fragment is too short or ambiguous to translate confidently on its "
            f"own (e.g. it's just the start of a name or a dangling word), return your "
            f"best reasonable guess rather than an empty string -- a slightly rough "
            f"partial is fine here, since the final authoritative translation is "
            f"computed separately once the whole sentence is done."
        )
        return self._call_gemini(prompt, _PartialTranslationResult)

    def _transcribe_auto(self, pcm16_bytes: bytes) -> Optional[_AutoTranscriptionResult]:
        """Phase 11: one-shot transcript of a complete meeting utterance,
        always auto-detecting the spoken language (unlike
        _transcribe_and_translate, which only auto-detects when
        self._source_lang == "auto" -- meeting mode has no bound source
        language at all, see start_session's placeholder pair in
        meeting_handlers.py). No translation is requested here -- that's
        translate_final's job, called separately per target language."""
        # The allowed set is CLOSED, not exemplary. The original prompt
        # said "e.g. 'en', 'az', 'ru'", which invites the whole ISO 639-1
        # list -- and on a short or noisy utterance Gemini duly returned
        # things like 'fr' and 'ko'. meeting_handlers drops any language
        # outside settings.meeting_languages, so each of those hallucinated
        # codes silently threw away a real sentence somebody had spoken.
        # Constraining the choice is much more effective here than widening
        # the accepted set downstream would be.
        allowed = list(get_settings().meeting_languages)
        allowed_str = ", ".join(f"'{code}'" for code in allowed)

        prompt = (
            "First identify the spoken language in the attached audio. Then "
            "transcribe exactly what is said, in that same language.\n\n"
            f"The speaker is using one of these languages: {allowed_str}. "
            "Report which one in detected_language, using exactly that "
            "two-letter code and nothing else. Never report any other "
            "language code. If the audio is too short, too noisy or too "
            "unclear to choose between them, pick the one it most resembles "
            "rather than guessing at a language outside this list.\n\n"
            "Only if there is no discernible speech at all (silence, background "
            "noise, breathing) return an empty string for transcript and null "
            "for detected_language."
        )
        return self._call_gemini(prompt, _AutoTranscriptionResult, pcm16_bytes=pcm16_bytes)

    def _translate_standalone(
        self, text: str, source_lang: str, target_lang: str
    ) -> Optional[_PartialTranslationResult]:
        """Phase 11: one-shot, non-continuation translation of an already-
        final transcript into an explicit target_lang -- text-only, via the
        same _call_gemini(pcm16_bytes=None) path _translate_continuation
        already exercises (built for Phase 8). Unlike
        _translate_continuation, there's no "already committed" prefix to
        avoid repeating -- this is the whole utterance, translated once."""
        source_name = _LANGUAGE_NAMES.get(source_lang, source_lang)
        target_name = _LANGUAGE_NAMES.get(target_lang, target_lang)
        prompt = (
            f"Translate the following {source_name} text into {target_name}.\n\n"
            f"Text to translate: \"{text}\"\n\n"
            f"Translate the way a skilled human interpreter would in a live business "
            f"meeting, not the way a document translator would: natural, idiomatic, "
            f"spoken {target_name}, phrased the way a native {target_name} speaker "
            f"would actually say it out loud in conversation. Prefer the most natural "
            f"spoken phrasing over a literal, word-for-word rendering of the source "
            f"sentence structure -- reorder words, drop filler, or rephrase as needed "
            f"for that, the way an interpreter does, as long as the meaning stays the "
            f"same. Avoid stiff, overly formal, or bookish wording.\n\n"
            f"Keep numbers, dates, times, personal names, and company/product names "
            f"exactly as they refer to -- never translate, guess at, or alter what "
            f"they mean (only reformat them into {target_name}'s normal written "
            f"convention if that differs, e.g. date order or a decimal separator)."
        )
        return self._call_gemini(prompt, _PartialTranslationResult)

    def _synthesize_chunk(self, text: str) -> Optional[bytes]:
        """
        Step 6, best-effort: calls Gemini's native text-to-speech model.
        This is a *different* API surface than _call_gemini's Interactions
        API above -- speech generation currently goes through
        `client.models.generate_content` with `response_modalities=["AUDIO"]`
        and a `speech_config` picking a prebuilt voice. The model speaks
        whatever language the input text is written in (no separate
        language parameter needed) -- since `text` here is already the
        *translated* text, it comes out in the target language.

        `contents` is deliberately NOT just the raw text. Handing a TTS-only
        model the bare translated phrase on its own (e.g. "Sure, that
        works." or "Yes.") sometimes reads to it as something to *reply to*
        rather than read aloud, and it tries to respond in text -- which a
        response_modalities=["AUDIO"] request isn't allowed to return, so
        Google rejects the whole call with a 400 ("Model tried to generate
        text, but it should only be used for TTS..."). Wrapping the text in
        an explicit "say exactly this" instruction is Google's own
        documented pattern for these models
        (https://ai.google.dev/gemini-api/docs/speech-generation) and keeps
        it in "read this aloud verbatim" mode instead.

        Confirmed against the live API (previously this couldn't be tested
        from the dev sandbox, which has no network access to Google's
        API) -- if it still errors on your machine, check
        GEMINI_TTS_MODEL/GEMINI_TTS_VOICE in config/.env against the
        current model names and voice list at the URL above. Either way, a
        failure here is caught and logged, never raised -- a chunk that
        fails to synthesize just isn't spoken; it doesn't break
        transcription/translation, which already succeeded by the time
        this runs.

        One failure mode you may still see and can ignore: Google's free
        tier currently caps this specific TTS model at 3 requests/minute
        (a 429 RESOURCE_EXHAUSTED, "quota exceeded ... limit: 3"). That's
        an account-level rate limit, not a bug -- it just means a chunk or
        two goes unspoken if you talk faster than that. It resolves itself
        after the minute rolls over, or by enabling billing on the Google
        AI Studio project for a higher quota.
        """
        try:
            from google.genai import types

            response = self._client.models.generate_content(
                model=self._tts_model,
                contents=f"Say exactly the following, and nothing else: {text}",
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self._tts_voice)
                        )
                    ),
                ),
            )
            candidates = response.candidates or []
            if not candidates or candidates[0].content is None or not candidates[0].content.parts:
                # Seen (rarely) with no clear error -- e.g. the model
                # declining for a safety/policy reason on this specific
                # chunk. finish_reason (if present) is the best clue;
                # logged as a warning rather than raised, same as every
                # other failure path here.
                finish_reason = getattr(candidates[0], "finish_reason", None) if candidates else None
                logger.warning(
                    "Gemini speech-generation returned no audio for a translated chunk (finish_reason=%s)",
                    finish_reason,
                )
                return None
            return candidates[0].content.parts[0].inline_data.data
        except Exception:
            logger.exception("Gemini speech-generation request failed for a translated chunk")
            return None

    def _pcm16_to_wav(self, pcm16_bytes: bytes) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self._sample_rate)
            wf.writeframes(pcm16_bytes)
        return buf.getvalue()

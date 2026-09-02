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
from typing import Optional, Type, TypeVar

from pydantic import BaseModel

from backend.translation.base import EventKind, TranslationEvent, TranslationProvider
from config.languages import SUPPORTED_LANGUAGES

logger = logging.getLogger(__name__)

_LANGUAGE_NAMES = {lang["code"]: lang["name"] for lang in SUPPORTED_LANGUAGES}

_ResultT = TypeVar("_ResultT", bound=BaseModel)


class _TranscriptionResult(BaseModel):
    transcript: str
    translation: str
    # ISO 639-1 code (e.g. "en"/"az"/"ru"), or null if Gemini couldn't tell.
    # Purely informational -- see TranslationEvent.detected_language in
    # base.py for why this never overrides the user's language selection.
    detected_language: Optional[str] = None


class _PartialTranscriptionResult(BaseModel):
    transcript: str


class GeminiTranslationProvider(TranslationProvider):
    def __init__(self, api_key: Optional[str], model: str, sample_rate: int) -> None:
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

    async def close_session(self) -> list[TranslationEvent]:
        return []

    # --- blocking helpers: always called via run_in_executor ---

    def _call_gemini(self, pcm16_bytes: bytes, prompt: str, schema_model: Type[_ResultT]) -> Optional[_ResultT]:
        try:
            interaction = self._client.interactions.create(
                model=self._model,
                input=[
                    self._TextContent(type="text", text=prompt),
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
                    ),
                ],
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
        source_name = _LANGUAGE_NAMES.get(self._source_lang, self._source_lang)
        target_name = _LANGUAGE_NAMES.get(self._target_lang, self._target_lang)

        prompt = (
            f"The attached audio is spoken in {source_name}. First transcribe exactly "
            f"what is said, in {source_name}. Then translate that transcript into "
            f"{target_name}. Also report the spoken language you detected as an ISO "
            "639-1 two-letter code (e.g. 'en', 'az', 'ru') in detected_language, or "
            "null if you can't tell. If the audio has no discernible speech (silence, "
            "noise, just breathing), return an empty string for both transcript and "
            "translation."
        )
        return self._call_gemini(pcm16_bytes, prompt, _TranscriptionResult)

    def _transcribe_only(self, pcm16_bytes: bytes) -> Optional[_PartialTranscriptionResult]:
        source_name = _LANGUAGE_NAMES.get(self._source_lang, self._source_lang)

        # Deliberately cheaper than _transcribe_and_translate: no
        # translation is requested, since partial results are for live
        # transcript feedback only and are never translated (see
        # TranslationProvider.transcribe_partial in base.py) -- this call
        # may run several times per utterance as speech continues, so
        # keeping it minimal matters more here than for the one-shot final.
        prompt = (
            f"The attached audio is spoken in {source_name}. Transcribe exactly what "
            f"is said so far, in {source_name} -- it may be a partial, unfinished "
            "sentence, that's expected. If there's no discernible speech yet, return "
            "an empty string."
        )
        return self._call_gemini(pcm16_bytes, prompt, _PartialTranscriptionResult)

    def _pcm16_to_wav(self, pcm16_bytes: bytes) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self._sample_rate)
            wf.writeframes(pcm16_bytes)
        return buf.getvalue()

"""
Google Gemini provider.

For each buffered audio chunk, sends the audio straight to Gemini (native
audio understanding -- no separate speech-to-text step) with instructions
to transcribe it in the source language and translate that transcript into
the target language, requesting structured JSON output so both fields come
back reliably instead of having to parse free-form text.

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
from typing import Optional

from pydantic import BaseModel

from backend.audio.vad import is_speech
from backend.translation.base import EventKind, TranslationEvent, TranslationProvider
from config.languages import SUPPORTED_LANGUAGES

logger = logging.getLogger(__name__)

_LANGUAGE_NAMES = {lang["code"]: lang["name"] for lang in SUPPORTED_LANGUAGES}


class _TranscriptionResult(BaseModel):
    transcript: str
    translation: str


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
        if not is_speech(pcm16_bytes):
            return []

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, self._transcribe_and_translate, pcm16_bytes)
        if result is None:
            return []

        transcript = result.transcript.strip()
        translation = result.translation.strip()
        if not transcript:
            return []

        return [
            TranslationEvent(kind=EventKind.TRANSCRIPT, text=transcript, is_final=True),
            TranslationEvent(kind=EventKind.TRANSLATION, text=translation, is_final=True),
        ]

    async def close_session(self) -> list[TranslationEvent]:
        return []

    def _transcribe_and_translate(self, pcm16_bytes: bytes) -> Optional[_TranscriptionResult]:
        source_name = _LANGUAGE_NAMES.get(self._source_lang, self._source_lang)
        target_name = _LANGUAGE_NAMES.get(self._target_lang, self._target_lang)

        prompt = (
            f"The attached audio is spoken in {source_name}. First transcribe exactly "
            f"what is said, in {source_name}. Then translate that transcript into "
            f"{target_name}. If the audio has no discernible speech (silence, noise, "
            "just breathing), return an empty string for both fields."
        )

        try:
            interaction = self._client.interactions.create(
                model=self._model,
                input=[
                    self._TextContent(type="text", text=prompt),
                    self._AudioContent(
                        type="audio",
                        data=base64.b64encode(self._pcm16_to_wav(pcm16_bytes)).decode("ascii"),
                        mime_type="audio/wav",
                    ),
                ],
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": _TranscriptionResult.model_json_schema(),
                },
            )
        except Exception:
            logger.exception("Gemini interactions.create request failed")
            return None

        try:
            return _TranscriptionResult.model_validate_json(interaction.output_text)
        except Exception:
            logger.exception("Could not parse Gemini response as the expected JSON shape: %r", interaction.output_text)
            return None

    def _pcm16_to_wav(self, pcm16_bytes: bytes) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self._sample_rate)
            wf.writeframes(pcm16_bytes)
        return buf.getvalue()

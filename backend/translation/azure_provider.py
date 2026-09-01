"""
Azure Speech Translation provider -- STUB.

Azure's Speech SDK has a purpose-built `TranslationRecognizer` that does
speech-to-text and translation in one streaming call, which maps well onto
`TranslationProvider` (see base.py): `start_session` would configure a
`SpeechTranslationConfig` for (source_lang, target_lang) and open a push
audio stream, `process_audio_chunk` would write bytes to that stream and
surface recognizing/recognized events, and `close_session` would close the
stream and stop the recognizer.

Requires: settings.azure_speech_key / azure_speech_region (see
config/settings.py, AZURE_SPEECH_KEY / AZURE_SPEECH_REGION in .env). Select
this provider by setting TRANSLATION_PROVIDER=azure.
"""

from __future__ import annotations

from backend.translation.base import TranslationEvent, TranslationProvider


class AzureSpeechTranslationProvider(TranslationProvider):
    def __init__(self, speech_key: str | None, region: str | None) -> None:
        if not speech_key or not region:
            raise ValueError(
                "AZURE_SPEECH_KEY and AZURE_SPEECH_REGION are required to use the "
                "azure translation provider"
            )
        self._speech_key = speech_key
        self._region = region

    async def start_session(self, source_lang: str, target_lang: str) -> None:
        raise NotImplementedError(
            "AzureSpeechTranslationProvider is a scaffold. Implement session setup "
            "against azure-cognitiveservices-speech here, then remove this guard."
        )

    async def process_audio_chunk(self, pcm16_bytes: bytes) -> list[TranslationEvent]:
        raise NotImplementedError

    async def close_session(self) -> list[TranslationEvent]:
        raise NotImplementedError

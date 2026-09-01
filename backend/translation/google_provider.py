"""
Google Cloud provider -- STUB.

Google's real-time path is two services chained together: streaming
Speech-to-Text produces interim/final transcripts, and each final
transcript is passed to the Cloud Translation API. `start_session` would
open a streaming recognize call configured for `source_lang`,
`process_audio_chunk` would push audio into that stream and, for each
final transcript it yields, call the Translation client to produce the
`target_lang` text, and `close_session` would close the streaming call.

Requires: settings.google_application_credentials (see config/settings.py,
GOOGLE_APPLICATION_CREDENTIALS in .env) pointing at a service account JSON
key. Select this provider by setting TRANSLATION_PROVIDER=google.
"""

from __future__ import annotations

from backend.translation.base import TranslationEvent, TranslationProvider


class GoogleCloudTranslationProvider(TranslationProvider):
    def __init__(self, credentials_path: str | None) -> None:
        if not credentials_path:
            raise ValueError(
                "GOOGLE_APPLICATION_CREDENTIALS is required to use the google "
                "translation provider"
            )
        self._credentials_path = credentials_path

    async def start_session(self, source_lang: str, target_lang: str) -> None:
        raise NotImplementedError(
            "GoogleCloudTranslationProvider is a scaffold. Implement streaming "
            "Speech-to-Text + Cloud Translation here, then remove this guard."
        )

    async def process_audio_chunk(self, pcm16_bytes: bytes) -> list[TranslationEvent]:
        raise NotImplementedError

    async def close_session(self) -> list[TranslationEvent]:
        raise NotImplementedError

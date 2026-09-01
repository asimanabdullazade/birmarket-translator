"""
OpenAI Realtime API provider -- STUB.

Sketches how a real streaming provider plugs into `TranslationProvider`
(see base.py). Not wired up to the actual API yet: implement `start_session`
to open a realtime session (e.g. via websockets to the Realtime API) with
instructions to transcribe `source_lang` and translate to `target_lang`,
implement `process_audio_chunk` to append audio to the session and drain
any transcript/translation deltas it emits, and `close_session` to commit
the final buffer and close the connection.

Requires: settings.openai_api_key (see config/settings.py, OPENAI_API_KEY
in .env). Select this provider by setting TRANSLATION_PROVIDER=openai.
"""

from __future__ import annotations

from backend.translation.base import TranslationEvent, TranslationProvider


class OpenAIRealtimeProvider(TranslationProvider):
    def __init__(self, api_key: str | None) -> None:
        if not api_key:
            raise ValueError(
                "OPENAI_API_KEY is required to use the openai translation provider"
            )
        self._api_key = api_key

    async def start_session(self, source_lang: str, target_lang: str) -> None:
        raise NotImplementedError(
            "OpenAIRealtimeProvider is a scaffold. Implement session setup against "
            "the OpenAI Realtime API here, then remove this guard."
        )

    async def process_audio_chunk(self, pcm16_bytes: bytes) -> list[TranslationEvent]:
        raise NotImplementedError

    async def close_session(self) -> list[TranslationEvent]:
        raise NotImplementedError

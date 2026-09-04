"""
Provider selection.

`get_provider()` is the single place that knows how to turn the
`TRANSLATION_PROVIDER` setting into a concrete `TranslationProvider`
instance. `websocket/handlers.py` calls this and otherwise only depends on
the abstract interface in `base.py`.

One provider name is an exception to that: "gemini_live" (Gemini Live
Translate, see backend/websocket/live_handlers.py) has no utterance
boundaries at all, so it can't be squeezed into `TranslationProvider`'s
"one complete utterance in, one atomic result out" shape (base.py) without
buffering a full VAD phrase before ever talking to it -- which would throw
away the entire reason to use it. It's handled by a second, parallel
connection-handling path (`run_live_session`) that `handlers.py` dispatches
to *before* ever calling `get_provider()`. `LIVE_PROVIDERS`/
`is_live_provider()` below are what let `handlers.py` (and this module's
own guard, see `get_provider`) know which provider names take that path,
without duplicating the list in more than one place.
"""

from __future__ import annotations

from config.settings import Settings
from backend.translation.base import TranslationProvider
from backend.translation.mock_provider import MockTranslationProvider

# Provider names handled by run_live_session (backend/websocket/
# live_handlers.py) instead of a TranslationProvider instance -- see the
# module docstring above.
LIVE_PROVIDERS = {"gemini_live"}


def is_live_provider(provider_name: str) -> bool:
    return provider_name.lower() in LIVE_PROVIDERS


def get_provider(settings: Settings) -> TranslationProvider:
    provider_name = settings.translation_provider.lower()

    if provider_name in LIVE_PROVIDERS:
        raise ValueError(
            f"'{provider_name}' is a streaming-mode provider handled directly by "
            "run_live_session (backend/websocket/live_handlers.py), not through "
            "get_provider() -- handlers.py should have dispatched to it via "
            "is_live_provider() before ever reaching this call."
        )

    if provider_name == "mock":
        return MockTranslationProvider()

    if provider_name == "local":
        from backend.translation.local_provider import LocalWhisperNLLBProvider

        return LocalWhisperNLLBProvider(
            whisper_model_size=settings.local_whisper_model_size,
            whisper_compute_type=settings.local_whisper_compute_type,
            nllb_model_repo=settings.local_nllb_model_repo,
            nllb_tokenizer_repo=settings.local_nllb_tokenizer_repo,
            nllb_compute_type=settings.local_nllb_compute_type,
        )

    if provider_name == "gemini":
        from backend.translation.gemini_provider import GeminiTranslationProvider

        return GeminiTranslationProvider(
            api_key=settings.gemini_api_key,
            model=settings.gemini_model,
            sample_rate=settings.audio_sample_rate,
            tts_model=settings.gemini_tts_model,
            tts_voice=settings.gemini_tts_voice,
        )

    if provider_name == "openai":
        from backend.translation.openai_provider import OpenAIRealtimeProvider

        return OpenAIRealtimeProvider(api_key=settings.openai_api_key)

    if provider_name == "azure":
        from backend.translation.azure_provider import AzureSpeechTranslationProvider

        return AzureSpeechTranslationProvider(
            speech_key=settings.azure_speech_key,
            region=settings.azure_speech_region,
            sample_rate=settings.audio_sample_rate,
        )

    if provider_name == "google":
        from backend.translation.google_provider import GoogleCloudTranslationProvider

        return GoogleCloudTranslationProvider(
            credentials_path=settings.google_application_credentials
        )

    raise ValueError(
        f"Unknown TRANSLATION_PROVIDER '{settings.translation_provider}'. "
        "Expected one of: mock, local, gemini, gemini_live, openai, azure, google."
    )

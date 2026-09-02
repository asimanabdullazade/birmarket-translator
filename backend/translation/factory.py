"""
Provider selection.

`get_provider()` is the single place that knows how to turn the
`TRANSLATION_PROVIDER` setting into a concrete `TranslationProvider`
instance. `websocket/handlers.py` calls this and otherwise only depends on
the abstract interface in `base.py`.
"""

from __future__ import annotations

from config.settings import Settings
from backend.translation.base import TranslationProvider
from backend.translation.mock_provider import MockTranslationProvider


def get_provider(settings: Settings) -> TranslationProvider:
    provider_name = settings.translation_provider.lower()

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
        )

    if provider_name == "openai":
        from backend.translation.openai_provider import OpenAIRealtimeProvider

        return OpenAIRealtimeProvider(api_key=settings.openai_api_key)

    if provider_name == "azure":
        from backend.translation.azure_provider import AzureSpeechTranslationProvider

        return AzureSpeechTranslationProvider(
            speech_key=settings.azure_speech_key, region=settings.azure_speech_region
        )

    if provider_name == "google":
        from backend.translation.google_provider import GoogleCloudTranslationProvider

        return GoogleCloudTranslationProvider(
            credentials_path=settings.google_application_credentials
        )

    raise ValueError(
        f"Unknown TRANSLATION_PROVIDER '{settings.translation_provider}'. "
        "Expected one of: mock, local, gemini, openai, azure, google."
    )

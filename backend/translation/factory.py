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
        "Expected one of: mock, openai, azure, google."
    )

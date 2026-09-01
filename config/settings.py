"""
Application settings, loaded from environment variables (see .env.example).

Uses pydantic-settings so values can come from a real environment or a
`.env` file in the config/ or backend/ directory. Nothing here is secret by
itself -- actual API keys belong in a local, git-ignored `.env` file.
"""

from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "config/.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Server ---
    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # --- Translation provider ---
    # "mock" works out of the box with no credentials and is the default so
    # the app is runnable immediately. Set to "openai", "azure", or "google"
    # (once those providers are implemented) and supply the matching keys.
    translation_provider: str = "mock"

    openai_api_key: Optional[str] = None
    azure_speech_key: Optional[str] = None
    azure_speech_region: Optional[str] = None
    google_application_credentials: Optional[str] = None

    # --- Audio ---
    # Must match the sample rate the frontend AudioWorklet resamples/encodes to.
    audio_sample_rate: int = 16000
    audio_channels: int = 1
    # Chunk size (seconds) buffered before a partial-translation pass is triggered.
    audio_chunk_seconds: float = 2.0

    # --- Logging ---
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()

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
    # "gemini" sends each audio chunk to Google's Gemini API (native audio
    # understanding) for transcription + translation in one call -- see
    # backend/translation/gemini_provider.py. Needs GEMINI_API_KEY below
    # (get one at https://aistudio.google.com/apikey; Gemini has a free
    # usage tier). "local" runs real speech-to-text + translation entirely
    # on your own machine instead (faster-whisper + Meta's NLLB-200, see
    # backend/translation/local_provider.py) -- no API key at all, but the
    # first run downloads ~1.4GB of models and every chunk after that runs
    # on your CPU. "mock" needs nothing but returns placeholder text, useful
    # for testing the plumbing without waiting on models or an API call. Set
    # to "openai", "azure", or "google" for one of the other paid vendor
    # APIs, and supply the matching key(s) below.
    translation_provider: str = "gemini"

    gemini_api_key: Optional[str] = None
    gemini_model: str = "gemini-flash-latest"

    openai_api_key: Optional[str] = None
    azure_speech_key: Optional[str] = None
    azure_speech_region: Optional[str] = None
    google_application_credentials: Optional[str] = None

    # --- Local provider (faster-whisper + NLLB-200) ---
    local_whisper_model_size: str = "base"  # tiny | base | small | medium | large-v3
    local_whisper_compute_type: str = "int8"  # int8 is fastest on CPU; try "float32" if quality suffers
    local_nllb_model_repo: str = "entai2965/nllb-200-distilled-600M-ctranslate2"
    local_nllb_tokenizer_repo: str = "facebook/nllb-200-distilled-600M"
    local_nllb_compute_type: str = "int8"

    # --- Audio ---
    # Must match the sample rate the frontend AudioWorklet resamples/encodes to.
    audio_sample_rate: int = 16000
    audio_channels: int = 1
    # Chunk size (seconds) buffered before a partial-translation pass is triggered.
    audio_chunk_seconds: float = 2.0

    # --- Logging ---
    log_level: str = "INFO"

    # --- Debugging / testing ---
    # When set, every session's raw incoming PCM16LE audio (exactly as
    # received over the WebSocket, before any provider-side chunking) is
    # written to a timestamped .wav file in this directory -- lets you
    # literally play back what the backend received, to check for
    # distortion, gaps, or duplicated audio. Leave unset in normal use.
    debug_audio_dump_dir: Optional[str] = None


@lru_cache
def get_settings() -> Settings:
    return Settings()

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
    # APIs, and supply the matching key(s) below. "gemini_live" is a
    # different shape entirely -- see the next paragraph.
    translation_provider: str = "gemini"

    gemini_api_key: Optional[str] = None
    gemini_model: str = "gemini-flash-latest"

    # Step 6 (text-to-speech). Google's model naming/availability for
    # speech generation moves faster than the main chat models -- if this
    # errors, check https://ai.google.dev/gemini-api/docs/speech-generation
    # for the current model name and voice list and override here.
    gemini_tts_model: str = "gemini-2.5-flash-preview-tts"
    gemini_tts_voice: str = "Kore"

    # "gemini_live" (TRANSLATION_PROVIDER=gemini_live): Gemini's *Live* API
    # -- one persistent connection, continuous audio in, continuous
    # translated audio out, no waiting for a complete utterance first --
    # rather than the request/response Interactions API above. Reuses
    # GEMINI_API_KEY; no separate credential. See "Using Gemini Live
    # Translate" in the README and backend/websocket/live_handlers.py for
    # the (substantial) architectural difference from every other provider
    # here. gemini_live_finalize_silence_ms/gemini_live_max_phrase_seconds
    # only matter as *fallbacks* -- live_handlers.py prefers a native
    # turn/transcription-completion signal from the API when one arrives;
    # these are the backstop for whenever that signal doesn't (see the
    # module docstring for why both exist).
    gemini_live_model: str = "gemini-3.5-live-translate-preview"
    gemini_live_finalize_silence_ms: float = 700.0
    gemini_live_max_phrase_seconds: float = 15.0

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

    # --- Voice Activity Detection (VAD) ---
    # Audio is split into phrases by detected speech, not fixed time
    # windows -- see backend/audio/segmenter.py. vad_threshold is the same
    # RMS-energy cutoff used by backend/audio/vad.py's is_speech().
    # vad_pre_speech_ms is how much audio to keep buffered *before* speech
    # is detected, so the first word/syllable isn't clipped.
    # vad_end_silence_ms is how long silence must persist before a phrase
    # is considered finished (400-700ms is a reasonable range: short
    # enough to feel responsive, long enough not to fragment on a normal
    # mid-sentence pause).
    vad_threshold: float = 0.01
    vad_pre_speech_ms: float = 400.0
    vad_end_silence_ms: float = 500.0
    # How often (ms) to surface an interim transcript while a phrase is
    # still being spoken -- see "Step 4" / backend/websocket/handlers.py.
    # Lower = more responsive partial updates but more provider calls.
    vad_partial_interval_ms: float = 700.0

    # --- Streaming translation (Phase 8) ---
    # Instead of waiting for a whole utterance, translate/speak the STABLE
    # part of a growing partial transcript as soon as it stops changing --
    # see backend/translation/stability.py and "Using streaming
    # translation" in the README. Only gemini/mock implement
    # TranslationProvider.translate_partial (local/azure keep today's
    # wait-for-final behavior regardless of this setting -- see the
    # README section above for why). Master toggle, so today's baseline
    # behavior is always one env var away for comparison/debugging.
    streaming_incremental_translation: bool = True
    # A word only "graduates" to committed once it's agreed on by two
    # consecutive partial transcripts, then this many of ITS trailing
    # words are additionally held back each round, as a safety margin
    # against the next partial revising something that only looked stable
    # by chance (transcribe_partial re-transcribes from scratch each round
    # -- it's not a monotonic streaming decoder). Tune from your own logs
    # (see the README) the same way VAD_END_SILENCE_MS was tuned.
    streaming_stability_holdback_words: int = 2
    # Minimum newly-stabilized word count worth bothering to translate+speak
    # as an increment -- avoids chattering the API for single-word commits.
    streaming_min_commit_words: int = 3
    # At finalization, only trust (and skip re-speaking) the incrementally
    # committed translation if it agrees with at least this fraction of
    # itself against the authoritative final translation -- otherwise the
    # whole final translation is spoken instead, exactly like pre-Phase-8
    # behavior. See reconcile_final_tts_text in stability.py for why this
    # is deliberately a binary fallback, not a fine-grained diff.
    streaming_final_reconcile_min_coverage: float = 0.9

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

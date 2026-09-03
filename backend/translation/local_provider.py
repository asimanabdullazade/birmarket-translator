"""
Local, free, no-API-key translation provider.

Runs both halves of the pipeline entirely on your own machine, with no
vendor account and no per-request cost:

  - Speech-to-text: faster-whisper, a CTranslate2 reimplementation of
    OpenAI's open-source Whisper model. Whisper's own language list
    includes English ("en"), Azerbaijani ("az"), and Russian ("ru").
  - Translation: Meta's open-source NLLB-200 model (distilled 600M,
    pre-converted to CTranslate2 format by a community repo), which covers
    all three languages via their FLORES-200 codes (eng_Latn / azj_Latn /
    rus_Cyrl).

Both models are downloaded once from Hugging Face on first use --
Whisper's "base" model is roughly 150MB, and the NLLB 600M weights are
roughly 1.2GB -- and cached under your Hugging Face cache dir (usually
~/.cache/huggingface) afterwards. There's no per-request cost or API key,
ever, but that first run needs a working internet connection and a few
minutes, and every chunk after that runs on your own CPU rather than a
managed API, so expect noticeably higher latency and lower accuracy than
a real vendor speech-translation service -- see openai_provider.py /
azure_provider.py / google_provider.py for those, when you're ready to pay
for better quality/latency.

This is the default provider (TRANSLATION_PROVIDER=local in config/.env)
so the app produces real transcripts/translations out of the box.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import AsyncIterator, Optional

import numpy as np

from backend.audio.processor import AudioBuffer
from backend.translation.base import EventKind, TranslationEvent, TranslationProvider

logger = logging.getLogger(__name__)

# FLORES-200 codes NLLB expects, keyed by this app's own language codes
# (config/languages.py). Add an entry here for every new language you add
# to config/languages.py, once you've confirmed NLLB has a FLORES code for it.
NLLB_LANG_CODES = {
    "en": "eng_Latn",
    "az": "azj_Latn",
    "ru": "rus_Cyrl",
}

# Module-level singletons: loading these models is slow and memory-heavy,
# so every session in this process reuses the same instances rather than
# reloading per WebSocket connection. The lock only guards the *loading*
# step (a race on first use); the loaded objects themselves are read-only
# during inference except for `tokenizer.src_lang` -- see the note in
# _translate() below about concurrent sessions with different source
# languages.
_load_lock = threading.Lock()
_whisper_model = None
_nllb_translator = None
_nllb_tokenizer = None


def _get_whisper_model(model_size: str, compute_type: str):
    global _whisper_model
    if _whisper_model is None:
        with _load_lock:
            if _whisper_model is None:
                from faster_whisper import WhisperModel

                logger.info("Loading faster-whisper model '%s' (%s)...", model_size, compute_type)
                _whisper_model = WhisperModel(model_size, device="cpu", compute_type=compute_type)
    return _whisper_model


def _get_nllb(model_repo_id: str, tokenizer_repo_id: str, compute_type: str):
    global _nllb_translator, _nllb_tokenizer
    if _nllb_translator is None:
        with _load_lock:
            if _nllb_translator is None:
                import ctranslate2
                from huggingface_hub import snapshot_download
                from transformers import AutoTokenizer

                logger.info("Downloading/loading NLLB model '%s' (%s)...", model_repo_id, compute_type)
                model_dir = snapshot_download(model_repo_id)
                _nllb_translator = ctranslate2.Translator(model_dir, device="cpu", compute_type=compute_type)
                # Deliberately loaded from the ORIGINAL facebook/nllb-200-*
                # repo, not the CTranslate2-converted one -- the converted
                # repo only carries model weights, not the sentencepiece
                # vocab the tokenizer needs. This matches CTranslate2's own
                # documented usage pattern for NLLB.
                _nllb_tokenizer = AutoTokenizer.from_pretrained(tokenizer_repo_id)
    return _nllb_translator, _nllb_tokenizer


class LocalWhisperNLLBProvider(TranslationProvider):
    """Free, local speech-to-text (faster-whisper) + translation (NLLB-200)."""

    def __init__(
        self,
        whisper_model_size: str = "base",
        whisper_compute_type: str = "int8",
        nllb_model_repo: str = "entai2965/nllb-200-distilled-600M-ctranslate2",
        nllb_tokenizer_repo: str = "facebook/nllb-200-distilled-600M",
        nllb_compute_type: str = "int8",
    ) -> None:
        self._whisper_model_size = whisper_model_size
        self._whisper_compute_type = whisper_compute_type
        self._nllb_model_repo = nllb_model_repo
        self._nllb_tokenizer_repo = nllb_tokenizer_repo
        self._nllb_compute_type = nllb_compute_type
        self._source_lang = "en"
        self._target_lang = "az"

    async def start_session(self, source_lang: str, target_lang: str) -> None:
        if source_lang not in NLLB_LANG_CODES or target_lang not in NLLB_LANG_CODES:
            raise ValueError(
                f"Local provider has no NLLB mapping for '{source_lang}' -> '{target_lang}'. "
                "Add both codes to NLLB_LANG_CODES in local_provider.py."
            )
        self._source_lang = source_lang
        self._target_lang = target_lang

        loop = asyncio.get_event_loop()
        # First call in the process blocks on a download + load (see the
        # module docstring for expected size/time); later calls just reuse
        # the already-loaded singletons. Run off the event loop either way
        # so a slow first load doesn't stall other coroutines.
        await loop.run_in_executor(
            None, _get_whisper_model, self._whisper_model_size, self._whisper_compute_type
        )
        await loop.run_in_executor(
            None, _get_nllb, self._nllb_model_repo, self._nllb_tokenizer_repo, self._nllb_compute_type
        )

    async def process_audio_chunk(self, pcm16_bytes: bytes) -> list[TranslationEvent]:
        audio = AudioBuffer.to_numpy(pcm16_bytes)
        if audio.size == 0:
            return []

        loop = asyncio.get_event_loop()
        transcript = (await loop.run_in_executor(None, self._transcribe, audio)).strip()
        if not transcript:
            return []

        translation = (await loop.run_in_executor(None, self._translate, transcript)).strip()

        return [
            TranslationEvent(kind=EventKind.TRANSCRIPT, text=transcript, is_final=True),
            TranslationEvent(kind=EventKind.TRANSLATION, text=translation, is_final=True),
        ]

    async def transcribe_partial(self, pcm16_bytes: bytes) -> Optional[str]:
        audio = AudioBuffer.to_numpy(pcm16_bytes)
        if audio.size == 0:
            return None
        loop = asyncio.get_event_loop()
        # Reuses the same (forced-language, see _transcribe's docstring)
        # decode as the final pass, just re-run on however much audio has
        # accumulated so far -- simple, correct, but means partial cost
        # scales with how much of the phrase has been spoken, not just the
        # newest bit of it. Fine for the short phrases this app targets.
        transcript = (await loop.run_in_executor(None, self._transcribe, audio)).strip()
        return transcript or None

    async def synthesize_speech(self, text: str) -> AsyncIterator[tuple[bytes, int]]:
        # Deliberately not implemented (Step 6): there's no good, actually-
        # free offline TTS engine with real Azerbaijani/Russian voice
        # support to match this provider's free/local/no-API-key promise --
        # the common offline options (e.g. pyttsx3) only speak whatever
        # voices your OS happens to have installed, which for most systems
        # means no Azerbaijani at all and patchy Russian. Rather than
        # silently produce English-accented gibberish for those languages,
        # this provider simply yields no audio -- the translated text still
        # shows up in the transcript history either way (see Step 4), it
        # just isn't spoken aloud. Use TRANSLATION_PROVIDER=gemini for real
        # synthesized speech.
        return
        yield b"", 0  # pragma: no cover -- unreachable; makes this an async generator

    async def close_session(self) -> list[TranslationEvent]:
        return []

    # --- blocking helpers: always called via run_in_executor, never awaited directly ---

    def _transcribe(self, audio: np.ndarray) -> str:
        model = _get_whisper_model(self._whisper_model_size, self._whisper_compute_type)
        # `language` is forced to the user's UI selection rather than left
        # as None for auto-detection -- Whisper's auto-detect is noticeably
        # less reliable on short clips (a few seconds) than being told the
        # language outright, and this app already has the user pick source
        # language explicitly, so there's nothing to gain and accuracy to
        # lose by second-guessing it here. If you want a genuine detected
        # language signal from this provider, pass language=None instead
        # and read info.language/info.language_probability -- but expect
        # more misfires on short utterances. gemini_provider.py surfaces
        # real detection instead, at no extra cost (same API call already
        # being made), if you want that without the accuracy trade-off.
        segments, _info = model.transcribe(audio, language=self._source_lang, vad_filter=True)
        return " ".join(segment.text for segment in segments)

    def _translate(self, text: str) -> str:
        # NOTE (Step 5): unlike gemini_provider.py, there's no prompt here --
        # NLLB is a fixed sequence-to-sequence translation model, not an
        # instruction-following LLM, so it can't be asked to prefer natural
        # spoken phrasing over a literal one, or to explicitly preserve
        # numbers/dates/names verbatim. In practice it's a reasonably
        # literal, general-purpose MT model: numbers usually survive intact,
        # but proper nouns can get transliterated or mistranslated, and its
        # register leans toward standard written language rather than
        # casual speech. If natural-sounding, spoken-register translation
        # matters more than staying fully free/local/offline, use
        # TRANSLATION_PROVIDER=gemini instead.
        translator, tokenizer = _get_nllb(
            self._nllb_model_repo, self._nllb_tokenizer_repo, self._nllb_compute_type
        )
        target_prefix = [NLLB_LANG_CODES[self._target_lang]]

        # NOTE: `tokenizer` is a shared singleton (see above), and setting
        # `src_lang` mutates shared state. That's safe for one session at a
        # time -- the common case for this scaffold -- but if you run
        # multiple concurrent sessions with *different* source languages,
        # give each session its own AutoTokenizer.from_pretrained(...)
        # instance instead of sharing this one, to avoid a race.
        tokenizer.src_lang = NLLB_LANG_CODES[self._source_lang]
        source_tokens = tokenizer.convert_ids_to_tokens(tokenizer.encode(text))

        results = translator.translate_batch([source_tokens], target_prefix=[target_prefix])
        output_tokens = results[0].hypotheses[0][1:]  # drop the leading target-language token
        return tokenizer.decode(tokenizer.convert_tokens_to_ids(output_tokens))

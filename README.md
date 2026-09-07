# Real-Time Speech Translator

A live speech-to-speech(ish) translator: capture microphone audio in the
browser, stream it to a FastAPI backend over a WebSocket, and get back a
live transcript and translation.

```
translator/
├── frontend/          React (Vite) UI
│   ├── public/pcmWorkletProcessor.js   AudioWorklet: mic audio -> PCM16
│   └── src/
│       ├── components/                LanguageSelector, DeviceSelector, StatusIndicator, Controls
│       ├── hooks/                     useAudioDevices, useTranslationSession (WebSocket)
│       ├── audio/                     MicCapture (Web Audio API)
│       └── App.jsx
│
├── backend/           Python + FastAPI
│   ├── audio/          AudioBuffer (chunking), simple VAD
│   ├── translation/    Pluggable TranslationProvider interface
│   │   ├── base.py           the interface
│   │   ├── gemini_provider.py default: Google Gemini API (needs your own key)
│   │   ├── local_provider.py  real, free, no key, fully local (faster-whisper + NLLB-200)
│   │   ├── mock_provider.py  zero-dependency placeholder text, for testing plumbing
│   │   ├── openai_provider.py / azure_provider.py / google_provider.py   other paid-API stubs
│   │   └── factory.py        picks a provider from TRANSLATION_PROVIDER
│   ├── websocket/       Connection manager + per-connection protocol handler
│   ├── models/          Pydantic message schemas
│   └── main.py           FastAPI app / WebSocket route
│
└── config/
    ├── settings.py       env-driven app settings (pydantic-settings)
    ├── languages.py       shared list of supported languages
    └── .env.example
```

## How it works

1. The browser opens a WebSocket to `/ws/translate` and sends a `start`
   message with the chosen source/target language.
2. An `AudioWorklet` (`public/pcmWorkletProcessor.js`) converts the mic's
   audio into 16kHz mono PCM16 frames and streams them to the backend as
   binary WebSocket frames.
3. The backend buffers audio (`backend/audio/processor.py`) into
   fixed-size chunks and hands each one to a `TranslationProvider`
   (`backend/translation/base.py`).
4. The provider returns transcript/translation events, which the backend
   relays back to the browser as JSON messages, along with status updates
   (`connected` / `listening` / `translating` / `error`).

The app ships with `TRANSLATION_PROVIDER=gemini` by default, which sends
each audio chunk to Google's Gemini API to transcribe and translate in one
call -- see "Using Gemini" below for how to add your key. Set
`TRANSLATION_PROVIDER=local` instead for real translation with no API key
at all (runs on your own CPU, see "Real translation, for free"), or
`TRANSLATION_PROVIDER=mock` if you just want to exercise the WebSocket/UI
plumbing without any model or API call -- it returns placeholder text
based on simple audio-energy detection.

## Using Gemini

1. Get a key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
   (Gemini has a free usage tier).
2. Paste it into `config/.env` as `GEMINI_API_KEY=...` (copy
   `config/.env.example` to `config/.env` first if you haven't). That file
   is git-ignored -- your key won't get committed -- but it's worth a
   `git status` before your first commit to double check.
3. `pip install -r backend/requirements.txt` (installs `google-genai`).

Each ~2-second audio chunk (`AUDIO_CHUNK_SECONDS` in `config/.env`) is sent
to Gemini's native audio understanding with a prompt asking it to
transcribe in the source language and translate into the target language,
requesting structured JSON output so both come back reliably. Default
model is `gemini-flash-latest` (an alias Google keeps pointed at their
current flash model); override with `GEMINI_MODEL` if you want a specific
version instead. Note: this uses the SDK's "Interactions" API
(`client.interactions.create`), which the `google-genai` package itself
currently flags as experimental -- if a future SDK version changes its
shape, `gemini_provider.py` is the one file that would need updating.

## Running it

### Backend

```bash
cd translator
python -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
cp config/.env.example config/.env   # optional, defaults already work
uvicorn backend.main:app --reload --port 8000
```

### Frontend

```bash
cd translator/frontend
npm install
npm run dev
```

Open the printed local URL (typically `http://localhost:5173`). Grant
microphone permission when prompted, pick your languages/devices, and hit
**Start Translation**.

## Real translation, for free


`TRANSLATION_PROVIDER=local` (the default) runs two open-source models on
your own CPU, no account or key required:

- **Speech-to-text:** [faster-whisper](https://github.com/SYSTRAN/faster-whisper),
  a CTranslate2 build of OpenAI's open-source Whisper model.
- **Translation:** Meta's open-source [NLLB-200](https://ai.meta.com/research/no-language-left-behind/)
  (distilled 600M checkpoint), via a pre-converted CTranslate2 model.

**First run only:** both models download from Hugging Face the first time
the backend actually translates something (i.e. the first WebSocket
session, not at server startup) -- roughly 150MB for Whisper "base" and
~1.2GB for NLLB, cached under `~/.cache/huggingface` afterwards. That
first session will pause for a minute or two while this downloads; every
session after that (and every subsequent run of the app) reuses the cache
and starts instantly. This needs a normal, unrestricted internet
connection for that one download -- if your network blocks
`huggingface.co` (e.g. a locked-down corporate proxy), switch to
`TRANSLATION_PROVIDER=mock` to test everything else, or use one of the
paid providers below instead.

Trade-offs versus a paid vendor API: it's chunk-by-chunk (every
`AUDIO_CHUNK_SECONDS`, default 2s) rather than true continuous streaming,
and both latency and accuracy depend on your CPU -- expect noticeably
slower, rougher results than a managed speech-translation service,
especially on a laptop without a fast CPU. Tune it via `config/.env`:
`LOCAL_WHISPER_MODEL_SIZE` (`tiny`/`base`/`small`/`medium`/`large-v3` --
bigger is slower but more accurate) and `LOCAL_WHISPER_COMPUTE_TYPE` /
`LOCAL_NLLB_COMPUTE_TYPE` (`int8` is fastest on CPU).

## Wiring up a paid translation API

`backend/translation/base.py` defines the `TranslationProvider` interface
(`start_session` / `process_audio_chunk` / `close_session`). Three stub
subclasses are ready to fill in once you want a paid, managed service
instead of the free local one:

- `openai_provider.py` -- OpenAI Realtime API
- `azure_provider.py` -- Azure Speech Translation (`TranslationRecognizer`)
- `google_provider.py` -- Google Cloud Speech-to-Text + Translation API

Implement one, set `TRANSLATION_PROVIDER` (in `config/.env`) to `openai`,
`azure`, or `google`, add the matching API key/credentials, and
`backend/translation/factory.py` will pick it up automatically -- nothing
else in the app needs to change.

## Notes on the initial UI

The current UI intentionally only exposes: source language, target
language, microphone selector, output-device selector, Start/Stop, and a
status line (Connected / Listening / Translating / Error). Playback of
translated audio through the selected output device, richer transcript
history, and auth are all left for later iterations.

## Testing the microphone pipeline (Step 2: clean mic input)

Before worrying about translation quality, verify the capture/streaming
pipeline itself is clean:

1. In `config/.env`, set `TRANSLATION_PROVIDER=mock` (isolates this test
   from any translation API/model) and `DEBUG_AUDIO_DUMP_DIR=./debug_audio`.
2. Restart the backend, start the frontend, hit **Start Translation**, and
   say: *"Hello, this is a test."* Let it keep listening for a few minutes
   (podcast audio, music, silence -- whatever's around) before hitting Stop.
3. Check the backend terminal: no `Possible audio gap` or `duplicate audio
   frame` warnings should appear during normal speech.
4. Open the `.wav` file written to `debug_audio/` (backend logs its exact
   path when the session starts) in any audio player. It should sound
   exactly like what you said -- no pitch shift, no clicks/static, no
   repeated segments, no missing sections.

What's actually being tested: the frontend's `AudioWorklet`
(`frontend/public/pcmWorkletProcessor.js`) requests a 16kHz mono
`AudioContext`, but some browsers/OS audio stacks silently ignore that and
keep running at their hardware's native rate (e.g. 48kHz) -- if that
happened and went uncorrected, the recording would sound sped-up/pitched
("chipmunk" distortion). The worklet detects this via the
`sampleRate` it's actually given and resamples on the fly, logging
`[MicCapture] Resampling ...` to the browser console if it kicks in. It also
batches raw audio into ~200ms chunks (`AUDIO_SEND_CHUNK_MS` in
`frontend/src/config.js`) before sending each one over the WebSocket --
small enough for low latency, large enough not to flood the socket with a
message every few milliseconds. The backend's gap/duplicate-frame checks
and `.wav` dump (`backend/websocket/handlers.py`) exist purely to make this
step verifiable; leave `DEBUG_AUDIO_DUMP_DIR` unset for normal use.

## Testing the microphone pipeline (Step 2: clean mic input)

Before worrying about translation quality, verify the capture/streaming
pipeline itself is clean:

1. In `config/.env`, set `TRANSLATION_PROVIDER=mock` (isolates this test
   from any translation API/model) and `DEBUG_AUDIO_DUMP_DIR=./debug_audio`.
2. Restart the backend, start the frontend, hit **Start Translation**, and
   say: *"Hello, this is a test."* Let it keep listening for a few minutes
   (podcast audio, music, silence -- whatever's around) before hitting Stop.
3. Check the backend terminal: no `Possible audio gap` or `duplicate audio
   frame` warnings should appear during normal speech.
4. Open the `.wav` file written to `debug_audio/` (backend logs its exact
   path when the session starts) in any audio player. It should sound
   exactly like what you said -- no pitch shift, no clicks/static, no
   repeated segments, no missing sections.

What's actually being tested: the frontend's `AudioWorklet`
(`frontend/public/pcmWorkletProcessor.js`) requests a 16kHz mono
`AudioContext`, but some browsers/OS audio stacks silently ignore that and
keep running at their hardware's native rate (e.g. 48kHz) -- if that
happened and went uncorrected, the recording would sound sped-up/pitched
("chipmunk" distortion). The worklet detects this via the
`sampleRate` it's actually given and resamples on the fly, logging
`[MicCapture] Resampling ...` to the browser console if it kicks in. It also
batches raw audio into ~200ms chunks (`AUDIO_SEND_CHUNK_MS` in
`frontend/src/config.js`) before sending each one over the WebSocket --
small enough for low latency, large enough not to flood the socket with a
message every few milliseconds. The backend's gap/duplicate-frame checks
and `.wav` dump (`backend/websocket/handlers.py`) exist purely to make this
step verifiable; leave `DEBUG_AUDIO_DUMP_DIR` unset for normal use.

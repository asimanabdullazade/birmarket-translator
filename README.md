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
│   │   ├── mock_provider.py  default, zero-credential implementation
│   │   ├── openai_provider.py / azure_provider.py / google_provider.py   stubs
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

The app ships with `TRANSLATION_PROVIDER=mock` by default, so **it runs
end-to-end with no API keys** -- the mock provider uses simple audio-energy
detection to decide when you're speaking and returns placeholder
transcript/translation text. Swap in a real provider (see below) when
you're ready to wire up an actual speech translation API.

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

## Wiring up a real translation API

`backend/translation/base.py` defines the `TranslationProvider` interface
(`start_session` / `process_audio_chunk` / `close_session`). Three stub
subclasses are ready to fill in:

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

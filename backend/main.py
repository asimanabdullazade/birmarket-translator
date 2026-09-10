"""
FastAPI application entrypoint.

Run with:
    uvicorn backend.main:app --reload --port 8000
(from the translator/ project root, so the `backend` and `config` packages
both resolve).

Exposes:
  GET  /health           basic liveness check
  GET  /languages         supported languages, for the frontend dropdowns
  WS   /ws/translate       the audio/translation streaming protocol
  WS   /ws/meeting/{meeting_id}/ingest   Phase 11: meeting broadcast mode --
                            single audio source for one meeting (a real bot
                            or _dev_stream_meeting_audio.py)
  WS   /ws/meeting/{meeting_id}/listen   Phase 11: one companion-page
                            listener, subscribed to one language via ?lang=
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from backend.websocket.handlers import handle_connection
from backend.websocket.manager import ConnectionManager
from backend.websocket.meeting_handlers import handle_meeting_ingest, handle_meeting_listener
from backend.websocket.meeting_registry import MeetingRegistry
from config.languages import SUPPORTED_LANGUAGES
from config.settings import get_settings

settings = get_settings()

logging.basicConfig(level=settings.log_level)

app = FastAPI(title="Real-Time Speech Translator")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

manager = ConnectionManager()
# Phase 11 (meeting broadcast mode): one shared registry for the process's
# lifetime, passed explicitly into both handlers below rather than a
# hidden global inside meeting_registry.py -- see MeetingRegistry's
# docstring for why (tests construct their own fresh instance).
meeting_registry = MeetingRegistry()


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "active_connections": manager.active_count}


@app.get("/languages")
async def languages() -> list[dict]:
    return SUPPORTED_LANGUAGES


@app.websocket("/ws/translate")
async def ws_translate(websocket: WebSocket) -> None:
    await manager.connect(websocket)
    try:
        await handle_connection(websocket, settings)
    finally:
        manager.disconnect(websocket)


@app.websocket("/ws/meeting/{meeting_id}/ingest")
async def ws_meeting_ingest(websocket: WebSocket, meeting_id: str) -> None:
    await websocket.accept()
    await handle_meeting_ingest(websocket, settings, meeting_registry, meeting_id)


@app.websocket("/ws/meeting/{meeting_id}/listen")
async def ws_meeting_listen(websocket: WebSocket, meeting_id: str, lang: str) -> None:
    await websocket.accept()
    await handle_meeting_listener(websocket, settings, meeting_registry, meeting_id, lang)

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
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from backend.websocket.handlers import handle_connection
from backend.websocket.manager import ConnectionManager
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

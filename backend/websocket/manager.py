"""
Tracks active WebSocket connections.

A single translation session is one connection, so this is a thin registry
today -- but keeping it separate from `handlers.py` leaves room to add
things like a server-wide connection cap or broadcast/status endpoints
without touching the per-connection protocol logic.
"""

from __future__ import annotations

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    @property
    def active_count(self) -> int:
        return len(self._connections)

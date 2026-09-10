"""
Phase 11 (meeting broadcast mode): in-memory broadcast registry.

Two responsibilities, kept together because they share the same
`meeting_id` keyspace:

1. **Rooms.** Each `(meeting_id, lang)` pair maps to the set of listener
   WebSockets currently subscribed to that language in that meeting --
   see `add_listener`/`remove_listener`/`broadcast`. `broadcast` writes
   the same already-computed payload to every socket in a room; it never
   calls back into the translation provider itself (that only ever
   happens once, in meeting_handlers.py's `handle_meeting_ingest`,
   independent of how many listeners are in any room) -- this is what
   keeps provider-call volume flat regardless of listener count (see the
   Phase 11 plan's "bounded call count" section).
2. **Ingest claims.** At most one ingest connection may be live for a
   given `meeting_id` at a time (mixing two simultaneous audio sources
   into one segmenter would produce garbage) -- `try_claim_ingest`/
   `release_ingest` enforce that.

Plain in-memory Python structures, no external dependencies (Redis etc.
is a later scaling concern, not needed at the 100-200 listener scale this
phase targets on one process). One instance is created once in
`backend/main.py` and passed explicitly into both new handler functions
-- not a hidden module-level global -- so tests can construct a fresh
`MeetingRegistry()` per test, exactly like the existing `ConnectionManager`
pattern this project already uses elsewhere.
"""

from __future__ import annotations

import logging
from typing import Dict, Set, Tuple

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class MeetingRegistry:
    def __init__(self) -> None:
        self._rooms: Dict[Tuple[str, str], Set[WebSocket]] = {}
        self._active_ingests: Set[str] = set()

    def add_listener(self, meeting_id: str, lang: str, websocket: WebSocket) -> None:
        self._rooms.setdefault((meeting_id, lang), set()).add(websocket)

    def remove_listener(self, meeting_id: str, lang: str, websocket: WebSocket) -> None:
        room = self._rooms.get((meeting_id, lang))
        if room is None:
            return
        room.discard(websocket)
        if not room:
            # Tidy up empty rooms rather than letting the dict grow forever
            # across many short-lived meetings.
            del self._rooms[(meeting_id, lang)]

    def listener_count(self, meeting_id: str, lang: str) -> int:
        return len(self._rooms.get((meeting_id, lang), ()))

    async def broadcast(self, meeting_id: str, lang: str, payload: str) -> None:
        """Send `payload` (already-serialized JSON) to every listener
        currently in (meeting_id, lang). A socket whose send() fails is
        dropped from the room rather than aborting the whole broadcast --
        same dead-socket tolerance as handlers.py's `_safe_send`, applied
        per recipient instead of per single-listener session."""
        room = self._rooms.get((meeting_id, lang))
        if not room:
            return
        dead: list = []
        for websocket in room:
            try:
                await websocket.send_text(payload)
            except Exception:
                dead.append(websocket)
        for websocket in dead:
            room.discard(websocket)
        if not room:
            del self._rooms[(meeting_id, lang)]

    def try_claim_ingest(self, meeting_id: str) -> bool:
        """Returns False if an ingest is already live for this meeting."""
        if meeting_id in self._active_ingests:
            return False
        self._active_ingests.add(meeting_id)
        return True

    def release_ingest(self, meeting_id: str) -> None:
        self._active_ingests.discard(meeting_id)

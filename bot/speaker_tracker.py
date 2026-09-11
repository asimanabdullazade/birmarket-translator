"""
Phase 14: works out who is currently talking, by reading it off the Teams
page the bot already has open.

WHY THE DOM AND NOT THE AUDIO
-----------------------------
The bot captures a single mixed PCM stream from PulseAudio -- every
participant's voice already blended into one signal with no metadata.
Recovering "who spoke" from that means speaker diarization: a model, real
latency, and anonymous labels ("Speaker 1") that still have to be matched
to actual people somehow. Teams, meanwhile, is already displaying the
answer with the correct human name on it. Reading the roster is far
cheaper and gives real names immediately.

The trade-off is honest: this is best-effort. The indicator lags a beat
behind real speech, overlapping talkers resolve to whoever Teams decided
was dominant, and a rendering change upstream can break the selectors.
Attribution is therefore always optional -- `None` is a normal value and
the whole pipeline works without it, exactly as it did in Phase 13.

SELECTORS ARE UNCONFIRMED
-------------------------
Unlike the join flow (whose selectors were pinned from a real control
dump), these are written against what Teams *generally* does, because the
roster wasn't captured while the bot was in a meeting. dump_candidates()
exists to fix that in one run: it writes every plausible speaking-related
element to JSON so the real selector can be read off rather than guessed
at, the same workflow that cracked the join in Phase 12.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from playwright.async_api import Page

# Strategies are tried in order and the first hit wins. Each returns a
# display name or null.
# Resolved from a real in-meeting control dump (unlike r1 of this file,
# which guessed). Two facts make this work:
#
#   1. Teams sets data-tid on each participant tile to the person's
#      DISPLAY NAME -- data-tid="Asiman Abdullazada". Odd, but it means a
#      tile is identifiable as "a div whose data-tid equals the first line
#      of its own text", which needs no class-name matching at all.
#   2. Each tile contains a [data-tid="voice-level-stream-outline"] -- the
#      ring Teams animates around whoever is talking.
#
# What a static dump CANNOT reveal is how that ring encodes "active":
# opacity, transform, colour, or a class swap. So rather than guess, this
# returns every tile's ring style and lets the Python side pick the odd
# one out, logging the raw values so the discriminator can be pinned
# exactly if the heuristic is wrong.
_ACTIVE_SPEAKER_JS = r"""
() => {
  // data-tid values that are structural, not people's names.
  const STRUCTURAL = new Set([
    "participant-avatar", "stage-layout", "modern-stage-wrapper",
    "stage-layouts-renderer", "MixedStage-wrapper", "calling-pagination",
    "only-videos-wrapper", "voice-level-stream-outline", "ai-interpreter-outline",
    "calling-right-side-panel", "rail-header", "right-side-panel-header-title",
    "rail-header-close-button", "meeting-branding-v9-provider",
    "meeting-branding-v0-provider", "toolbar-item-badge", "calling-slot-background"
  ]);

  const firstLine = (s) => {
    const text = (s || "").trim();
    const nl = text.indexOf(String.fromCharCode(10));
    return (nl === -1 ? text : text.slice(0, nl)).trim();
  };

  const tiles = [];
  document.querySelectorAll("div[data-tid]").forEach((el) => {
    const tid = el.getAttribute("data-tid");
    if (!tid || STRUCTURAL.has(tid)) return;
    // The tile whose data-tid IS its own label: that's a person.
    if (firstLine(el.innerText) === tid.trim()) tiles.push({ name: tid, el: el });
  });

  return tiles.map((t) => {
    const ring = t.el.querySelector('[data-tid="voice-level-stream-outline"]');
    let style = null;
    if (ring) {
      const cs = getComputedStyle(ring);
      style = [
        cs.opacity, cs.transform, cs.borderColor, cs.display,
        cs.visibility, (cs.boxShadow || "").slice(0, 40),
        (ring.className || "").toString().slice(0, 80)
      ].join("|");
    }
    return { name: t.name, hasRing: !!ring, style: style };
  });
}
"""


_CANDIDATE_DUMP_JS = r"""
() => {
  const out = [];
  const sel = [
    '[aria-label*="speak" i]', '[title*="speak" i]',
    '[data-tid*="speak" i]', '[class*="speak" i]',
    '[data-tid*="participant" i]', '[data-tid*="roster" i]',
    '[class*="dominant" i]', '[class*="activeSpeaker" i]',
    '[data-tid="voice-level-stream-outline"]', '[data-tid="ai-interpreter-outline"]',
    'div[data-tid]'
  ].join(", ");
  document.querySelectorAll(sel).forEach((el) => {
    const r = el.getBoundingClientRect();
    out.push({
      tag: el.tagName.toLowerCase(),
      dataTid: el.getAttribute("data-tid"),
      ariaLabel: el.getAttribute("aria-label"),
      title: el.getAttribute("title"),
      cls: (el.className || "").toString().slice(0, 140),
      text: (el.innerText || "").trim().slice(0, 80),
      visible: r.width > 0 && r.height > 0
    });
  });
  return out;
}
"""


class SpeakerTracker:
    # How many times a tile must be seen in one style before that style is
    # trusted as its idle baseline. At 0.5s polling this is ~2 seconds.
    _WARMUP_OBSERVATIONS = 4
    # Observation count at which a tile's style history is halved.
    _DECAY_AT = 60
    # Consecutive identical polls required before publishing a
    # speaker change. At 0.5s polling this is ~1s of agreement.
    _STABLE_POLLS = 2

    """
    Polls the Teams page for the active speaker.

    `current` is read by the ingest client, which forwards changes to the
    backend. Deliberately a plain attribute rather than a queue: the value
    is a piece of sticky state, not a stream of events, and the ingest
    loop is already sending audio frames several times a second, so it can
    piggyback the change onto the next frame.
    """

    def __init__(self, page: Page, settings, logger: logging.Logger) -> None:
        self._page = page
        self._settings = settings
        self._logger = logger
        self.current: Optional[str] = None
        # name -> {style_string: times_seen}. See _pick_speaker.
        self._baselines: dict = {}
        self._pending: Optional[str] = None
        self._pending_count = 0
        self._via: Optional[str] = None
        self._dumped = False

    async def dump_candidates(self, step_name: str = "speaker-candidates") -> None:
        """Write every plausible speaking-related element to JSON. Never raises."""
        try:
            directory = self._settings.screenshot_dir
            Path(directory).mkdir(parents=True, exist_ok=True)
            data = await self._page.evaluate(_CANDIDATE_DUMP_JS)
            path = os.path.join(directory, f"{step_name}-{time.strftime('%Y%m%d-%H%M%S')}.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"url": self._page.url, "candidates": data}, handle, indent=2, ensure_ascii=False)
            self._logger.info("speaker candidate dump: %s (%d elements)", path, len(data))
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("failed to dump speaker candidates: %s", exc)

    def _pick_speaker(self, tiles: list) -> Optional[str]:
        """
        Choose the talking participant by comparing each tile's voice-level
        ring against that tile's OWN learned baseline.

        A static DOM dump can show that the ring exists but not how it
        encodes "active" -- opacity, transform, colour, or a class swap.
        Rather than hard-code a guess, this observes: for each participant,
        the style seen most often is by definition their idle appearance
        (people are silent far more than they talk), and anything else is
        them speaking. It self-calibrates, and survives Teams changing
        which property it animates.

        An earlier version instead picked "the tile that differs from the
        others". That reads well but is wrong: with exactly two people
        every style is unique so nothing is ever the odd one out, and with
        two people talking at once it confidently returns the one person
        who is silent. Both were caught by the unit cases at the bottom of
        this file rather than in a meeting.

        Returns None during warm-up and whenever nobody deviates -- the
        normal state while nobody is speaking.
        """
        usable = [
            t for t in tiles
            if t.get("hasRing") and t.get("style")
            # The bot has a tile too and never speaks.
            and t.get("name") != self._settings.bot_display_name
        ]
        if not usable:
            return None

        candidates = []
        for tile in usable:
            name, style = tile["name"], tile["style"]
            seen = self._baselines.setdefault(name, {})
            seen[style] = seen.get(style, 0) + 1

            # Decay old observations so the baseline tracks RECENT
            # appearance rather than the whole session. Without this a
            # permanent change to someone's tile -- they turn their camera
            # on, the layout reflows -- looks like a deviation for minutes,
            # and they read as perpetually speaking. Halving on a cap keeps
            # roughly the last ~30s dominant at 0.5s polling.
            if seen[style] >= self._DECAY_AT:
                for key in list(seen):
                    seen[key] //= 2
                    if seen[key] <= 0:
                        del seen[key]
                seen[style] = max(seen.get(style, 0), 1)

            baseline_style, baseline_count = max(seen.items(), key=lambda kv: kv[1])
            # Warm-up: until a tile has been observed in one style enough
            # times, we don't know what its idle looks like, and calling
            # everything a deviation would attribute every caption to
            # whoever rendered first.
            if baseline_count < self._WARMUP_OBSERVATIONS:
                continue
            if style != baseline_style:
                candidates.append((seen[style], name))

        if not candidates:
            return None
        # Several deviating at once (people talking over each other):
        # prefer the rarest style, which is the strongest signal.
        candidates.sort()
        return candidates[0][1]

    async def run(self, stop_event: asyncio.Event, interval_s: float = 0.5) -> None:
        """Poll until stop_event. Never raises -- attribution is optional."""
        # One dump on startup so the real selectors are recoverable from a
        # single run even if every strategy below misses.
        await self.dump_candidates("speaker-candidates-initial")

        misses = 0
        while not stop_event.is_set():
            try:
                tiles = await self._page.evaluate(_ACTIVE_SPEAKER_JS) or []
                name = self._pick_speaker(tiles)

                # DEBOUNCE. A single poll is not evidence. The ring style
                # flickers -- a name appears, drops to None, reappears --
                # several times a second, and publishing every flip meant
                # downstream consumers saw a speaker change every couple of
                # words. Require the same answer N polls running before
                # believing it.
                if name == self._pending:
                    self._pending_count += 1
                else:
                    self._pending = name
                    self._pending_count = 1

                if self._pending_count >= self._STABLE_POLLS and name != self.current:
                    self._logger.info("active speaker: %r (of %d tiles)", name, len(tiles))
                    self.current = name

                if name is None:
                    misses += 1
                    # Every ~10s while unresolved, show what was actually
                    # seen. Without this the only visible state is "no
                    # names", which is consistent with a dozen different
                    # causes (no tiles, no rings, all styles identical,
                    # still warming up, picker raising).
                    if misses % 20 == 0:
                        self._logger.info(
                            "still no active speaker after %d polls; tiles=%s baselines=%s",
                            misses,
                            [(t.get("name"), t.get("hasRing"), (t.get("style") or "")[:60]) for t in tiles],
                            {n: dict(list(v.items())[:3]) for n, v in self._baselines.items()},
                        )
                    # If nothing ever matches, say so once and dump again
                    # while people are actually talking -- the startup dump
                    # is taken before anyone has spoken, so the speaking
                    # indicator wouldn't be in the DOM yet.
                    if misses == 60 and not self._dumped:
                        self._dumped = True
                        self._logger.warning(
                            "no active speaker detected in ~30s of polling -- the selectors in "
                            "speaker_tracker.py likely don't match this Teams build. Dumping candidates; "
                            "read the JSON and pin _ACTIVE_SPEAKER_JS to what's actually there."
                        )
                        await self.dump_candidates("speaker-candidates-nomatch")
                else:
                    misses = 0
            except Exception as exc:  # noqa: BLE001
                # NOT debug. This was debug-level once, and when the picker
                # started throwing, the symptom was simply "names stopped
                # appearing" with nothing in the log at default verbosity.
                # A diagnostic that hides its own failure is worse than none.
                self._logger.warning("speaker poll failed: %s", exc, exc_info=True)

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
            except asyncio.TimeoutError:
                continue

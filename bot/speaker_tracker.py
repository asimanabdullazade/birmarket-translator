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
answer with the correct human name on it. Reading the DOM is far cheaper
and gives real names immediately.

The trade-off is honest: this is best-effort. The indicator lags a beat
behind real speech, overlapping talkers resolve to one name, and a
rendering change upstream can break it. Attribution is therefore always
optional -- `None` is a normal value and the whole pipeline works without
it, exactly as it did in Phase 13.
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

# Resolved from a real in-meeting control dump. Two facts make this work:
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
# returns every tile's ring style and lets the Python side work it out,
# logging the raw values so the discriminator can be pinned exactly if
# the heuristic proves wrong.
_ACTIVE_SPEAKER_JS = """
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

  const tiles = [];
  document.querySelectorAll("div[data-tid]").forEach((el) => {
    const tid = el.getAttribute("data-tid");
    if (!tid || STRUCTURAL.has(tid)) return;
    const first = ((el.innerText || "").trim().split("\\n")[0] || "").trim();
    // The tile whose data-tid IS its own label: that's a person.
    if (first && first === tid) tiles.push({ name: tid, el: el });
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

# Everything that might plausibly carry the signal, for discovery.
_CANDIDATE_DUMP_JS = """
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

    """
    Polls the Teams page for the active speaker.

    `current` is read by the ingest client, which forwards changes to the
    backend. Deliberately a plain attribute rather than a queue: the value
    is sticky state, not a stream of events, and the ingest loop is
    already sending audio frames several times a second, so it can
    piggyback the change onto the next frame.
    """

    def __init__(self, page: Page, settings, logger: logging.Logger) -> None:
        self._page = page
        self._settings = settings
        self._logger = logger
        self.current: Optional[str] = None
        # name -> {style_string: times_seen}. See _pick_speaker.
        self._baselines: dict = {}
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
        who is silent. Both were caught by unit cases rather than in a
        meeting.

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
        # single run even if every strategy above misses.
        await self.dump_candidates("speaker-candidates-initial")

        misses = 0
        while not stop_event.is_set():
            try:
                tiles = await self._page.evaluate(_ACTIVE_SPEAKER_JS) or []
                name = self._pick_speaker(tiles)

                if name != self.current:
                    self._logger.info("active speaker: %r (of %d tiles)", name, len(tiles))
                    self.current = name

                if name is None:
                    misses += 1
                    # If nothing ever matches, say so once and dump again
                    # while people are actually talking -- the startup dump
                    # is taken before anyone has spoken, so the speaking
                    # indicator wouldn't be in the DOM yet.
                    if misses == 120 and not self._dumped:
                        self._dumped = True
                        self._logger.warning(
                            "no active speaker detected in ~60s of polling. Raw tile styles: %s",
                            [(t.get("name"), t.get("style")) for t in tiles],
                        )
                        await self.dump_candidates("speaker-candidates-nomatch")
                else:
                    misses = 0
            except Exception as exc:  # noqa: BLE001
                self._logger.debug("speaker poll failed: %s", exc)

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
            except asyncio.TimeoutError:
                continue

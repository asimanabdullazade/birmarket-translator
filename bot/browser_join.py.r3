"""
All Microsoft Teams DOM interaction lives here -- the one module in this
bot expected to need real-world iteration against the live Teams UI.

REVISION HISTORY (each entry is a lesson from a real failed run, kept so
the same mistake isn't reintroduced):

r1 -- written blind, never having loaded a real Teams page.

r2 -- first real runs reached pre-join, clicked "Join now", then sat for
the full lobby_timeout_s while Teams was already showing "Sorry, we
couldn't connect you." Added fail-fast failure-text detection, a polling
click helper with one shared budget (the old nested loops multiplied a
60s timeout by every (text, role) pair -- six wasted minutes), page-text
dumps on failure, and browser console/pageerror forwarding.

r3 -- r2 tried to force the camera off by withholding the browser's
camera permission. That BACKFIRED: Teams requests audio and video in a
single getUserMedia call, so denying video denied the microphone too.
Every media request failed with NotAllowedError, and the Join button
stayed disabled, so the run died at "could not find/click a 'Join now'
button". DO NOT withhold the camera permission. Both permissions are
granted now and the camera is hidden at the JS layer instead -- see
_HIDE_CAMERA_INIT_SCRIPT. r3 also learned, from that run's logged URL,
that this meeting link lands on Teams' *light-meetings* experience
(/light-meetings/launch?...&lightExperience=true), which goes straight
to pre-join with no app-launcher interstitial at all, and whose DOM may
not match anything documented for the full Teams web client. Hence
dump_controls().

Prefer role/text locators and data-tid attributes over CSS class
selectors -- Teams' class names are minified and change often.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from config import BotSettings
from logging_utils import save_screenshot

# --- Selectors / text patterns -------------------------------------------
# The block most likely to need updating against a real meeting.
#
# data-tid values are tried BEFORE visible text: Teams uses them as
# stable test hooks, so they survive UI/locale changes that break text
# matching. The ones below come from public writeups about the full Teams
# web client and are NOT confirmed against the light-meetings experience
# this bot actually lands on -- run once and read the *-controls-*.json
# dump to replace them with the real ones.

_NAME_INPUT_TIDS = ["prejoin-display-name-input"]
_NAME_INPUT_PLACEHOLDER_RE = re.compile(r"name", re.IGNORECASE)

_JOIN_BUTTON_TIDS = ["prejoin-join-button"]
_JOIN_NOW_TEXTS = ["Join now", "Join meeting", "Join"]

_CAMERA_TOGGLE_TIDS = ["toggle-video"]
_CAMERA_TOGGLE_NAMES = ["camera", "video"]

_MIC_TOGGLE_TIDS = ["toggle-mute"]
_MIC_TOGGLE_NAMES = ["mic", "microphone"]

_LEAVE_BUTTON_TIDS = ["hangup-button", "callingButtons-hangupButton"]
_LEAVE_CALL_TEXTS = ["Leave", "Leave call", "Hang up", "Leave meeting"]

_CONTINUE_ON_BROWSER_TEXTS = [
    "Continue on this browser",
    "Join on the web instead",
    "Use the web app instead",
    "Continue in this browser",
]

_LOBBY_TEXT_PATTERNS = [
    re.compile(r"waiting for someone to let you in", re.IGNORECASE),
    re.compile(r"someone in the meeting should let you in soon", re.IGNORECASE),
    re.compile(r"you're in the lobby", re.IGNORECASE),
    re.compile(r"waiting in the lobby", re.IGNORECASE),
]

# Text that means the join has definitively FAILED. Checked on every
# admission poll so a failure surfaces in seconds rather than after the
# full lobby_timeout_s.
_JOIN_FAILURE_TEXT_PATTERNS = [
    re.compile(r"couldn'?t connect you", re.IGNORECASE),
    re.compile(r"couldn'?t join", re.IGNORECASE),
    re.compile(r"we ran into a problem", re.IGNORECASE),
    re.compile(r"something went wrong", re.IGNORECASE),
    re.compile(r"you('| ha)ve been removed", re.IGNORECASE),
    re.compile(r"were denied|weren'?t admitted|declined your request", re.IGNORECASE),
]

_MEETING_ENDED_TEXT_PATTERNS = [
    re.compile(r"you('| ha)ve left", re.IGNORECASE),
    re.compile(r"the meeting has ended", re.IGNORECASE),
    re.compile(r"call ended", re.IGNORECASE),
]

# Documented-but-brittle URL query-param trick to skip the app-launcher
# interstitial. Only used if neither the pre-join form nor a
# continue-on-browser control shows up.
_BROWSER_JOIN_PARAMS = {
    "msLaunch": "0",
    "type": "meetup-join",
    "directDl": "true",
    "enableMobilePage": "true",
    "suppressPrompt": "true",
}


# --- Camera suppression ---------------------------------------------------
# Injected into every frame before any page script runs.
#
# WHY THIS AND NOT A PERMISSION DENIAL (see r3 in the module docstring):
# Teams calls getUserMedia once with BOTH audio and video constraints. If
# the camera permission is missing, that single call rejects with
# NotAllowedError and the bot loses its microphone as collateral damage,
# leaving the pre-join screen in an error state with Join disabled.
#
# Instead: report no videoinput devices and quietly drop the video
# constraint. Teams sees a perfectly normal mic-only machine, gets a
# working audio stream, shows the camera as unavailable, and never
# negotiates a video track -- which also sidesteps the H.264 question,
# since Playwright's bundled Chromium before v1.57 is the open-source
# build with no H.264 encoder.
_HIDE_CAMERA_INIT_SCRIPT = """
(() => {
  const md = navigator.mediaDevices;
  if (!md) return;

  const origEnumerate = md.enumerateDevices.bind(md);
  md.enumerateDevices = async () => {
    const devices = await origEnumerate();
    return devices.filter((d) => d.kind !== 'videoinput');
  };

  const origGum = md.getUserMedia.bind(md);
  md.getUserMedia = (constraints) => {
    const c = Object.assign({}, constraints || {});
    if (c.video) {
      delete c.video;
    }
    if (!c.audio) {
      // Video-only request with nothing left to ask for. Reject the way a
      // machine with no webcam would, rather than passing an empty
      // constraints object to getUserMedia (which throws a TypeError that
      // Teams does not expect).
      return Promise.reject(
        new DOMException('Requested device not found', 'NotFoundError')
      );
    }
    return origGum(c);
  };
})();
"""


class JoinFailure(Exception):
    """Raised when the bot can't get into the meeting -- interstitial, pre-join, or lobby admission failed."""


def _with_browser_join_params(url: str) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query))
    query.update(_BROWSER_JOIN_PARAMS)
    return urlunparse(parsed._replace(query=urlencode(query)))


# --- Debugging helpers ----------------------------------------------------

_CONTROL_DUMP_JS = """
() => {
  const sel = 'button, a, input, select, textarea, [role="button"], ' +
              '[role="switch"], [role="checkbox"], [role="menuitem"], [data-tid]';
  const seen = new Set();
  const out = [];
  document.querySelectorAll(sel).forEach((el) => {
    if (seen.has(el)) return;
    seen.add(el);
    const r = el.getBoundingClientRect();
    out.push({
      tag: el.tagName.toLowerCase(),
      dataTid: el.getAttribute('data-tid'),
      role: el.getAttribute('role'),
      ariaLabel: el.getAttribute('aria-label'),
      ariaChecked: el.getAttribute('aria-checked'),
      ariaPressed: el.getAttribute('aria-pressed'),
      ariaDisabled: el.getAttribute('aria-disabled'),
      disabled: el.disabled === true,
      placeholder: el.getAttribute('placeholder'),
      title: el.getAttribute('title'),
      text: (el.innerText || el.value || '').trim().slice(0, 100),
      visible: r.width > 0 && r.height > 0,
      box: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)]
    });
  });
  return out;
}
"""


async def dump_controls(page: Page, step_name: str, screenshot_dir: str, logger: logging.Logger) -> None:
    """
    Write every interactive element on the page -- with its data-tid, ARIA
    state, disabled flag and visible text -- to a JSON file next to the
    screenshots.

    This exists because guessing Teams selectors one run at a time is
    slow and this bot lands on the light-meetings experience, for which
    no public selector documentation exists. One run of this dump should
    be enough to fix every constant at the top of this module at once.
    Never raises.
    """
    try:
        Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
        controls = await page.evaluate(_CONTROL_DUMP_JS)
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        path = os.path.join(screenshot_dir, f"{step_name}-controls-{timestamp}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"url": page.url, "controls": controls}, handle, indent=2, ensure_ascii=False)
        visible = [c for c in controls if c.get("visible")]
        logger.info("control dump: %s (%d elements, %d visible)", path, len(controls), len(visible))
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to dump controls for step %r: %s", step_name, exc)


async def dump_page_text(page: Page, step_name: str, screenshot_dir: str, logger: logging.Logger) -> None:
    """Write the page's visible innerText next to the screenshots. Never raises."""
    try:
        Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        path = os.path.join(screenshot_dir, f"{step_name}-{timestamp}.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(f"url: {page.url}\n\n{text}\n")
        logger.info("page text dumped: %s", path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("failed to dump page text for step %r: %s", step_name, exc)


async def _diagnose(page: Page, step_name: str, settings: BotSettings, logger: logging.Logger) -> None:
    """Screenshot + text dump + control dump, for any failure path."""
    await save_screenshot(page, step_name, settings.screenshot_dir, logger)
    await dump_page_text(page, step_name, settings.screenshot_dir, logger)
    await dump_controls(page, step_name, settings.screenshot_dir, logger)


async def _first_visible_pattern(page: Page, patterns: list, timeout_ms: int = 800):
    """Return the first pattern from `patterns` whose text is visible, or None."""
    for pattern in patterns:
        try:
            if await page.get_by_text(pattern).first.is_visible(timeout=timeout_ms):
                return pattern
        except Exception:  # noqa: BLE001
            continue
    return None


async def _settle(page: Page, logger: logging.Logger, seconds: float = 4.0) -> None:
    """
    Give Teams' SPA time to render after a navigation. networkidle is
    unusable here -- Teams holds long-poll connections open, so it never
    fires.
    """
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:  # noqa: BLE001
        logger.debug("domcontentloaded wait timed out; continuing anyway")
    await asyncio.sleep(seconds)


# --- Browser launch -------------------------------------------------------


async def launch_browser(playwright, settings: BotSettings):
    """
    Launch headed Chromium (NOT headless=True -- WebRTC/audio reliability
    in true headless mode is inconsistent) under the entrypoint's Xvfb
    display.

    Flag notes:
    - --use-fake-ui-for-media-stream auto-accepts media prompts; nobody is
      there to click them.
    - --use-fake-device-for-media-stream supplies a fake/silent mic, since
      the container has no real input device. This is also the code-level
      enforcement of "the bot never transmits real audio into the
      meeting": with this flag it is not possible for this process to
      send real mic input into Teams.
    - --disable-dev-shm-usage: /dev/shm defaults to 64MB in Docker, which
      Chromium's renderer routinely exhausts. A renderer crash mid-join is
      indistinguishable from a Teams-side connection failure by the time
      you see the screenshot.
    - --enable-unsafe-swiftshader: the previous run logged "Automatic
      fallback to software WebGL has been deprecated. Please use
      --enable-unsafe-swiftshader". There is no GPU under Xvfb, and Teams
      uses WebGL in the pre-join preview, so opt in explicitly rather than
      relying on a deprecated implicit fallback.

    BOTH camera and microphone permissions are granted -- see
    _HIDE_CAMERA_INIT_SCRIPT for why withholding the camera permission is
    actively harmful, and how the camera is suppressed instead.
    """
    args = [
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
        "--no-sandbox",  # containers commonly need this; the container itself is the sandbox boundary
        "--disable-dev-shm-usage",
        "--autoplay-policy=no-user-gesture-required",
        "--enable-unsafe-swiftshader",
    ]

    launch_kwargs = {"headless": False, "args": args}
    if settings.browser_channel:
        # e.g. "chrome" / "msedge" -- only needed if Teams turns out to
        # require H.264 even with video suppressed. Must actually be
        # installed in the image.
        launch_kwargs["channel"] = settings.browser_channel

    browser = await playwright.chromium.launch(**launch_kwargs)
    context = await browser.new_context(permissions=["camera", "microphone"])

    if not settings.join_with_camera:
        await context.add_init_script(_HIDE_CAMERA_INIT_SCRIPT)

    page = await context.new_page()

    # Surface browser-side errors in the bot's own log. Teams' media/WebRTC
    # failures appear here and nowhere else; without this the join is a
    # black box.
    blog = logging.getLogger("teams_bot.browser")
    page.on("console", lambda msg: blog.debug("console[%s]: %s", msg.type, msg.text))
    page.on("pageerror", lambda err: blog.warning("page error: %s", err))
    page.on("crash", lambda _: blog.error("PAGE CRASHED -- likely /dev/shm exhaustion or an OOM"))

    return browser, context, page


# --- Locator helpers ------------------------------------------------------


def _tid_locator(page: Page, tid: str):
    return page.locator(f'[data-tid="{tid}"]')


async def _click_by_tid(page: Page, tids: list[str], timeout_ms: int = 3000) -> bool:
    """Try clicking by data-tid. Preferred over text: stable across locale/UI changes."""
    for tid in tids:
        try:
            await _tid_locator(page, tid).first.click(timeout=timeout_ms)
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


async def _click_first_matching_text(
    page: Page, texts: list[str], total_timeout_ms: int, per_try_timeout_ms: int = 2000
) -> bool:
    """
    Click the first visible element matching any of `texts`, polling all
    (text, role) candidates within ONE overall budget. See r2 in the
    module docstring for why the budget is shared.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + (total_timeout_ms / 1000.0)

    while loop.time() < deadline:
        for text in texts:
            for role in ("button", "link"):
                try:
                    await page.get_by_role(role, name=text).first.click(timeout=per_try_timeout_ms)
                    return True
                except PlaywrightTimeoutError:
                    continue
                except Exception:  # noqa: BLE001
                    continue
        await asyncio.sleep(0.5)
    return False


async def _prejoin_name_input(page: Page):
    """Locate the display-name input by data-tid, falling back to placeholder."""
    for tid in _NAME_INPUT_TIDS:
        locator = _tid_locator(page, tid).first
        try:
            if await locator.is_visible(timeout=1000):
                return locator
        except Exception:  # noqa: BLE001
            continue
    return page.get_by_placeholder(_NAME_INPUT_PLACEHOLDER_RE).first


# --- Join flow ------------------------------------------------------------


async def bypass_interstitial(page: Page, join_url: str, settings: BotSettings, logger: logging.Logger) -> None:
    """
    Navigate to the meeting link and land on the browser join page.

    Races three outcomes rather than doing them in sequence: the pre-join
    form appearing (the light-meetings experience goes straight there and
    has no interstitial at all -- see r3), a continue-on-browser control
    appearing, or neither within the budget. The previous version checked
    for the pre-join form exactly once, 2s after navigating, before Teams
    had finished rendering -- so it always missed, then wasted 20s hunting
    for an interstitial that was never going to exist, then wasted more on
    a pointless fallback navigation.
    """
    logger.info("navigating to meeting join URL")
    await page.goto(join_url, timeout=int(settings.join_timeout_s * 1000))
    await _settle(page, logger)
    await save_screenshot(page, "after-initial-navigate", settings.screenshot_dir, logger)

    loop = asyncio.get_event_loop()
    deadline = loop.time() + 40.0

    while loop.time() < deadline:
        try:
            if await (await _prejoin_name_input(page)).is_visible(timeout=1000):
                logger.info("pre-join form is up -- no interstitial to bypass")
                return
        except Exception:  # noqa: BLE001
            pass

        for text in _CONTINUE_ON_BROWSER_TEXTS:
            for role in ("button", "link"):
                try:
                    await page.get_by_role(role, name=text).first.click(timeout=800)
                    logger.info("clicked a 'continue on this browser' style element (%r)", text)
                    await _settle(page, logger)
                    await save_screenshot(page, "after-interstitial-click", settings.screenshot_dir, logger)
                    return
                except Exception:  # noqa: BLE001
                    continue

        await asyncio.sleep(1.0)

    logger.warning(
        "neither the pre-join form nor a 'continue on this browser' control appeared within 40s -- "
        "falling back to the browser-join URL query-param trick (documented as brittle)"
    )
    await _diagnose(page, "interstitial-not-found", settings, logger)
    await page.goto(_with_browser_join_params(join_url), timeout=int(settings.join_timeout_s * 1000))
    await _settle(page, logger)
    await save_screenshot(page, "after-fallback-navigate", settings.screenshot_dir, logger)


async def _set_toggle_off(
    page: Page, tids: list[str], names: list[str], label: str, logger: logging.Logger
) -> bool:
    """
    Find a pre-join media toggle and switch it off if it's on.

    Tries data-tid first, then role+name across switch/checkbox/button --
    Teams has shipped this control as all three across rollouts, and state
    lives in aria-checked on a switch but aria-pressed on a button. r2's
    version only read aria-pressed on role="button", matched nothing, and
    silently left the camera on.

    Never blind-clicks a control whose state can't be read: that risks
    turning something ON.
    """
    candidates = [_tid_locator(page, tid).first for tid in tids]
    for name in names:
        pattern = re.compile(name, re.IGNORECASE)
        for role in ("switch", "checkbox", "button"):
            candidates.append(page.get_by_role(role, name=pattern).first)

    for locator in candidates:
        try:
            if not await locator.is_visible(timeout=800):
                continue
            state = await locator.get_attribute("aria-checked")
            if state is None:
                state = await locator.get_attribute("aria-pressed")

            if state == "false":
                logger.info("%s is already off", label)
                return True
            if state == "true":
                await locator.click(timeout=2000)
                logger.info("toggled %s off on the pre-join screen", label)
                return True
            logger.debug("%s control found but has no readable aria state", label)
        except Exception:  # noqa: BLE001
            continue

    logger.warning(
        "could not locate a %s toggle with readable state -- check the *-controls-*.json dump "
        "and update the _*_TOGGLE_TIDS constants",
        label,
    )
    return False


async def complete_prejoin(page: Page, settings: BotSettings, logger: logging.Logger) -> None:
    """Fill in the bot's display name, turn mic/camera off, click Join now."""
    logger.info("filling in pre-join display name: %r", settings.bot_display_name)

    name_input = await _prejoin_name_input(page)
    try:
        await name_input.fill(settings.bot_display_name, timeout=20000)
    except PlaywrightTimeoutError as exc:
        await _diagnose(page, "prejoin-name-input-not-found", settings, logger)
        raise JoinFailure(
            "could not find the pre-join display-name input -- see the screenshot and the "
            "*-controls-*.json dump beside it for the real selector"
        ) from exc

    # The camera is already suppressed at the JS layer (see
    # _HIDE_CAMERA_INIT_SCRIPT), so this is cosmetic belt-and-braces for
    # the participant list. Muting the mic is likewise cosmetic: the fake
    # device is silent regardless.
    if not settings.join_with_camera:
        await _set_toggle_off(page, _CAMERA_TOGGLE_TIDS, _CAMERA_TOGGLE_NAMES, "camera", logger)
    await _set_toggle_off(page, _MIC_TOGGLE_TIDS, _MIC_TOGGLE_NAMES, "microphone", logger)

    await save_screenshot(page, "prejoin-filled", settings.screenshot_dir, logger)
    # Always dumped, not just on failure: this is the page whose selectors
    # keep changing, and a successful run's dump is the reference for the
    # next time it breaks.
    await dump_controls(page, "prejoin-filled", settings.screenshot_dir, logger)

    clicked = await _click_by_tid(page, _JOIN_BUTTON_TIDS)
    if not clicked:
        clicked = await _click_first_matching_text(page, _JOIN_NOW_TEXTS, total_timeout_ms=30000)

    if not clicked:
        await _diagnose(page, "prejoin-join-button-not-found", settings, logger)
        raise JoinFailure(
            "could not find/click a 'Join now' button. NOTE: Playwright's click() waits for the "
            "element to be ENABLED, so a disabled Join button looks identical to a missing one -- "
            "check the 'disabled'/'ariaDisabled' fields in the *-controls-*.json dump. Teams keeps "
            "Join disabled while media initialization is failing."
        )

    logger.info("clicked join now")
    await _settle(page, logger, seconds=2.0)
    await save_screenshot(page, "after-join-click", settings.screenshot_dir, logger)


async def wait_for_admission(page: Page, settings: BotSettings, logger: logging.Logger) -> None:
    """
    Wait until the bot is actually in the meeting (a leave-call control is
    visible) rather than stuck in the lobby.

    Checks for explicit FAILURE text every poll and raises immediately --
    r1 sat here for the full 240s polling a page that had been showing
    "Sorry, we couldn't connect you." almost from the start, making a
    fast, loud failure look like a slow, silent one.
    """
    logger.info("waiting up to %.0fs to be admitted from the lobby (if there is one)", settings.lobby_timeout_s)

    loop = asyncio.get_event_loop()
    deadline = loop.time() + settings.lobby_timeout_s
    last_periodic_shot = 0.0
    lobby_logged = False

    while loop.time() < deadline:
        # 1. Hard failure -- bail out now, don't wait for the timeout.
        failure = await _first_visible_pattern(page, _JOIN_FAILURE_TEXT_PATTERNS)
        if failure is not None:
            await _diagnose(page, "join-error-detected", settings, logger)
            raise JoinFailure(
                f"Teams reported a join failure (matched {failure.pattern!r}). This is Teams refusing "
                "the connection, not a lobby wait. Check the browser console lines just above this "
                "for media/WebRTC errors."
            )

        # 2. Admitted?
        for tid in _LEAVE_BUTTON_TIDS:
            try:
                if await _tid_locator(page, tid).first.is_visible(timeout=500):
                    logger.info("admitted to the meeting (found data-tid=%r)", tid)
                    await save_screenshot(page, "admitted", settings.screenshot_dir, logger)
                    await dump_controls(page, "admitted", settings.screenshot_dir, logger)
                    return
            except Exception:  # noqa: BLE001
                continue
        for text in _LEAVE_CALL_TEXTS:
            try:
                if await page.get_by_role("button", name=text).first.is_visible(timeout=500):
                    logger.info("admitted to the meeting (found a %r control)", text)
                    await save_screenshot(page, "admitted", settings.screenshot_dir, logger)
                    await dump_controls(page, "admitted", settings.screenshot_dir, logger)
                    return
            except Exception:  # noqa: BLE001
                continue

        # 3. Still in the lobby? Log it once, informationally.
        if not lobby_logged:
            if await _first_visible_pattern(page, _LOBBY_TEXT_PATTERNS) is not None:
                lobby_logged = True
                logger.info("in the lobby, waiting to be admitted")
                await save_screenshot(page, "in-lobby", settings.screenshot_dir, logger)

        # 4. Periodic screenshot so the wait isn't a blind window.
        now = loop.time()
        if now - last_periodic_shot >= settings.admission_screenshot_interval_s:
            last_periodic_shot = now
            await save_screenshot(page, "awaiting-admission", settings.screenshot_dir, logger)

        await asyncio.sleep(2.0)

    await _diagnose(page, "admission-timeout", settings, logger)
    raise JoinFailure(
        f"was not admitted to the meeting within {settings.lobby_timeout_s:.0f}s -- either nobody let "
        "the bot in, or admission-detection didn't recognize the in-meeting UI. If the bot IS visibly "
        "in the meeting, find the real leave-button data-tid in the *-controls-*.json dump and add it "
        "to _LEAVE_BUTTON_TIDS."
    )


async def watch_for_meeting_end(
    page: Page, settings: BotSettings, logger: logging.Logger, stop_event: asyncio.Event
) -> None:
    """
    Poll for the meeting-ended/left screen and set stop_event when
    detected. Requires the same state across 2 consecutive polls so a
    transient render doesn't cut a session short.
    """
    consecutive_hits = 0
    required_consecutive_hits = 2

    while not stop_event.is_set():
        ended = await _first_visible_pattern(page, _MEETING_ENDED_TEXT_PATTERNS, timeout_ms=1000) is not None

        if ended:
            consecutive_hits += 1
            logger.info(
                "meeting-ended text detected (%d/%d consecutive checks)", consecutive_hits, required_consecutive_hits
            )
            if consecutive_hits >= required_consecutive_hits:
                logger.info("meeting ended -- triggering shutdown")
                await save_screenshot(page, "meeting-ended", settings.screenshot_dir, logger)
                stop_event.set()
                return
        else:
            consecutive_hits = 0

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.meeting_end_poll_interval_s)
        except asyncio.TimeoutError:
            continue

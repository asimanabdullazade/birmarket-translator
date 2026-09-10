"""
All Microsoft Teams DOM interaction lives here -- the one module in this
bot expected to need real-world iteration against the live Teams UI.

REVISION NOTES (after the first real-meeting runs failed):

The original version of this file was written without ever loading a real
Teams page. The first real runs got as far as the pre-join screen, clicked
"Join now", and then sat for the full lobby_timeout_s while the page was
already showing Teams' own "Sorry, we couldn't connect you." error. Four
changes came out of that:

1. The camera is now OFF by default, enforced at the permission level
   rather than by clicking a toggle. Teams needs H.264 to publish video;
   Playwright's bundled Chromium below v1.57 is the open-source build,
   which has no H.264. Publishing a fake video track it can't encode is
   the leading suspect for the connection failure. The old toggle-clicking
   code looked for aria-pressed on role="button" and matched nothing, so
   the camera was silently left on.
2. wait_for_admission now checks for FAILURE text every poll and raises
   immediately, instead of discovering the failure only via a timeout
   screenshot minutes later.
3. _click_first_matching_text polls with short per-attempt timeouts inside
   one overall budget. The old nested loop multiplied a 60s timeout by
   every (text, role) pair, so a missing element cost ~6 minutes.
4. Every failure path now dumps the page's visible text next to the
   screenshot, so selectors can be fixed from the actual DOM text rather
   than by squinting at a PNG.

Prefer Playwright's role/text-based locators (get_by_role, get_by_text,
filter) over CSS class selectors -- Teams' class names are minified and
change often; visible text and ARIA roles survive UI refreshes better,
though even these need adjustment over time.
"""

from __future__ import annotations

import asyncio
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
# The block most likely to need updating against a real meeting. Kept as
# named constants, not inlined, so updates are a one-line diff per
# element rather than a hunt through the flow functions below.

_CONTINUE_ON_BROWSER_TEXTS = [
    "Continue on this browser",
    "Join on the web instead",
    "Use the web app instead",
    "Continue in this browser",
]

_NAME_INPUT_PLACEHOLDER_RE = re.compile(r"name", re.IGNORECASE)

_JOIN_NOW_TEXTS = ["Join now", "Join meeting", "Join"]

_LOBBY_TEXT_PATTERNS = [
    re.compile(r"waiting for someone to let you in", re.IGNORECASE),
    re.compile(r"someone in the meeting should let you in soon", re.IGNORECASE),
    re.compile(r"you're in the lobby", re.IGNORECASE),
    re.compile(r"waiting in the lobby", re.IGNORECASE),
]

_LEAVE_CALL_TEXTS = ["Leave", "Leave call", "Hang up", "Leave meeting"]

# NEW: text that means the join has definitively FAILED. Checked on every
# admission poll so a failure surfaces in seconds rather than after the
# full lobby_timeout_s. "Sorry, we couldn't connect you." is the exact
# error the first real runs hit.
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

# Toggle labels on the pre-join screen. Teams renders these as switches,
# not plain buttons -- see _set_toggle_off.
_CAMERA_TOGGLE_NAMES = ["camera", "video"]
_MIC_TOGGLE_NAMES = ["mic", "microphone"]

# Documented-but-brittle URL query-param trick to skip straight to the
# browser join flow without the app-launcher interstitial appearing at
# all. Microsoft can change/break this at any time.
_BROWSER_JOIN_PARAMS = {
    "msLaunch": "0",
    "type": "meetup-join",
    "directDl": "true",
    "enableMobilePage": "true",
    "suppressPrompt": "true",
}


class JoinFailure(Exception):
    """Raised when the bot can't get into the meeting -- interstitial, pre-join, or lobby admission failed."""


def _with_browser_join_params(url: str) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query))
    query.update(_BROWSER_JOIN_PARAMS)
    return urlunparse(parsed._replace(query=urlencode(query)))


# --- Debugging helpers ----------------------------------------------------


async def dump_page_text(page: Page, step_name: str, screenshot_dir: str, logger: logging.Logger) -> None:
    """
    Write the page's visible innerText next to the screenshots.

    A PNG tells you the join broke; the text tells you what string to put
    in the constants above to fix it. Never raises -- this runs on failure
    paths that are already reporting a different error.
    """
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
    Give Teams' SPA a moment to actually render after a navigation.

    The first real run screenshotted a bare Teams splash logo immediately
    after goto() and then hunted for elements that hadn't rendered yet.
    networkidle is not usable here -- Teams keeps long-poll connections
    open, so it never fires.
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
    in true headless mode is inconsistent) under whatever DISPLAY the
    entrypoint's Xvfb set up.

    CAMERA POLICY: video is denied at the browser-permission level unless
    settings.join_with_camera is explicitly true. The old version passed
    --use-fake-ui-for-media-stream, which auto-accepts *every* media
    prompt including video, and then tried to click the camera toggle off
    afterwards. Denying the permission is enforcement rather than
    best-effort: getUserMedia({video:true}) simply rejects, so Teams
    enters the meeting camera-off and never attempts to negotiate an
    H.264 video track.

    --use-fake-device-for-media-stream is still required: there is no real
    microphone in the container, and Teams needs *some* enumerable input
    device. It also remains the code-level enforcement of "the bot never
    transmits real audio into the meeting."

    --disable-dev-shm-usage matters specifically in Docker: /dev/shm
    defaults to 64MB, which Chromium's renderer routinely exhausts. A
    renderer crash mid-join is indistinguishable from a Teams-side
    connection failure by the time you see the screenshot.
    """
    args = [
        "--use-fake-device-for-media-stream",
        "--no-sandbox",  # containers commonly need this; the container itself is the sandbox boundary
        "--disable-dev-shm-usage",
        "--autoplay-policy=no-user-gesture-required",
    ]
    if settings.join_with_camera:
        # Only in this mode do we auto-accept the camera prompt too.
        args.append("--use-fake-ui-for-media-stream")

    launch_kwargs = {"headless": False, "args": args}
    if settings.browser_channel:
        # e.g. "chrome" or "msedge" -- needed for H.264 on Playwright
        # <1.57, whose bundled Chromium is the codec-stripped open-source
        # build. Requires that channel to actually be installed in the
        # image; see the Dockerfile.
        launch_kwargs["channel"] = settings.browser_channel

    browser = await playwright.chromium.launch(**launch_kwargs)

    permissions = ["microphone"]
    if settings.join_with_camera:
        permissions.append("camera")
    context = await browser.new_context(permissions=permissions)
    page = await context.new_page()

    # Surface browser-side errors in the bot's own log. WebRTC/media
    # failures inside Teams show up here and nowhere else -- without this
    # the whole join is a black box between "clicked join" and "timed out".
    logger = logging.getLogger("teams_bot.browser")
    page.on("console", lambda msg: logger.debug("console[%s]: %s", msg.type, msg.text))
    page.on("pageerror", lambda err: logger.warning("page error: %s", err))
    page.on("crash", lambda _: logger.error("PAGE CRASHED -- likely /dev/shm exhaustion or an OOM"))

    return browser, context, page


# --- Join flow ------------------------------------------------------------


async def _click_first_matching_text(
    page: Page, texts: list[str], total_timeout_ms: int, per_try_timeout_ms: int = 2000
) -> bool:
    """
    Try clicking the first visible element matching any of `texts`.

    Polls all (text, role) candidates repeatedly within ONE overall budget.
    The previous implementation nested a full-length timeout inside each
    candidate, so a genuinely-absent element cost len(texts) * 2 * timeout
    -- six minutes in the first real run, before the flow had even started.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + (total_timeout_ms / 1000.0)

    while loop.time() < deadline:
        for text in texts:
            for role in ("button", "link"):
                locator = page.get_by_role(role, name=text)
                try:
                    await locator.first.click(timeout=per_try_timeout_ms)
                    return True
                except PlaywrightTimeoutError:
                    continue
                except Exception:  # noqa: BLE001
                    continue
        await asyncio.sleep(0.5)
    return False


async def bypass_interstitial(page: Page, join_url: str, settings: BotSettings, logger: logging.Logger) -> None:
    """
    Navigate to the meeting link and land on the browser join page.

    Primary path: click a "Continue on this browser"-style element if
    Teams shows the app-launcher interstitial. Fallback: re-navigate with
    the browser-join query params.
    """
    logger.info("navigating to meeting join URL")
    await page.goto(join_url, timeout=int(settings.join_timeout_s * 1000))
    await _settle(page, logger)
    await save_screenshot(page, "after-initial-navigate", settings.screenshot_dir, logger)

    # If the pre-join form is already on screen, there is no interstitial
    # to bypass -- skip straight ahead rather than burning the budget.
    try:
        if await page.get_by_placeholder(_NAME_INPUT_PLACEHOLDER_RE).first.is_visible(timeout=2000):
            logger.info("pre-join form already visible -- no interstitial to bypass")
            return
    except Exception:  # noqa: BLE001
        pass

    clicked = await _click_first_matching_text(page, _CONTINUE_ON_BROWSER_TEXTS, total_timeout_ms=20000)
    if clicked:
        logger.info("clicked a 'continue on this browser' style element")
        await _settle(page, logger)
        await save_screenshot(page, "after-interstitial-click", settings.screenshot_dir, logger)
        return

    logger.warning(
        "no 'continue on this browser' element found within 20s -- falling back to the browser-join "
        "URL query-param trick (documented as brittle, see this module's docstring)"
    )
    await dump_page_text(page, "interstitial-not-found", settings.screenshot_dir, logger)
    fallback_url = _with_browser_join_params(join_url)
    await page.goto(fallback_url, timeout=int(settings.join_timeout_s * 1000))
    await _settle(page, logger)
    await save_screenshot(page, "after-fallback-navigate", settings.screenshot_dir, logger)


async def _set_toggle_off(page: Page, names: list[str], label: str, logger: logging.Logger) -> bool:
    """
    Find a pre-join media toggle and switch it off if it's on.

    Teams renders these as switches, so aria-checked is the state
    attribute, not aria-pressed -- the original code checked only
    aria-pressed on role="button" and therefore never matched, silently
    leaving the camera on. All three roles are tried because Teams has
    shipped this control differently across rollouts.
    """
    for name in names:
        pattern = re.compile(name, re.IGNORECASE)
        for role in ("switch", "checkbox", "button"):
            locator = page.get_by_role(role, name=pattern).first
            try:
                if not await locator.is_visible(timeout=1000):
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
                # Present but no readable state -- leave it alone rather
                # than blind-clicking, which could turn something ON.
                logger.debug("%s control found via role=%s but has no readable state", label, role)
            except Exception:  # noqa: BLE001
                continue

    logger.warning("could not locate a %s toggle on the pre-join screen", label)
    return False


async def complete_prejoin(page: Page, settings: BotSettings, logger: logging.Logger) -> None:
    """Fill in the bot's display name, force mic/camera off, click Join now."""
    logger.info("filling in pre-join display name: %r", settings.bot_display_name)

    name_input = page.get_by_placeholder(_NAME_INPUT_PLACEHOLDER_RE).first
    try:
        await name_input.fill(settings.bot_display_name, timeout=int(settings.join_timeout_s * 1000))
    except PlaywrightTimeoutError as exc:
        await save_screenshot(page, "prejoin-name-input-not-found", settings.screenshot_dir, logger)
        await dump_page_text(page, "prejoin-name-input-not-found", settings.screenshot_dir, logger)
        raise JoinFailure(
            "could not find the pre-join display-name input -- Teams' pre-join screen layout may have "
            "changed, see the screenshot and the .txt dump beside it"
        ) from exc

    if not settings.join_with_camera:
        await _set_toggle_off(page, _CAMERA_TOGGLE_NAMES, "camera", logger)
    await _set_toggle_off(page, _MIC_TOGGLE_NAMES, "microphone", logger)

    await save_screenshot(page, "prejoin-filled", settings.screenshot_dir, logger)

    clicked = await _click_first_matching_text(page, _JOIN_NOW_TEXTS, total_timeout_ms=30000)
    if not clicked:
        await save_screenshot(page, "prejoin-join-button-not-found", settings.screenshot_dir, logger)
        await dump_page_text(page, "prejoin-join-button-not-found", settings.screenshot_dir, logger)
        raise JoinFailure("could not find/click a 'Join now' button on the pre-join screen, see the screenshot")

    logger.info("clicked join now")
    await _settle(page, logger, seconds=2.0)
    await save_screenshot(page, "after-join-click", settings.screenshot_dir, logger)


async def wait_for_admission(page: Page, settings: BotSettings, logger: logging.Logger) -> None:
    """
    Wait until the bot is actually in the meeting (a "leave call" control
    is visible) rather than stuck in the lobby.

    Unlike the original version, this checks for explicit FAILURE text on
    every poll and raises immediately when it appears. The first real runs
    sat here for the full 240s polling a page that had been showing
    "Sorry, we couldn't connect you." almost from the start -- the
    timeout made a fast, loud failure look like a slow, silent one.
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
            await save_screenshot(page, "join-error-detected", settings.screenshot_dir, logger)
            await dump_page_text(page, "join-error-detected", settings.screenshot_dir, logger)
            raise JoinFailure(
                f"Teams reported a join failure (matched {failure.pattern!r}). This is Teams refusing the "
                "connection, not a lobby wait. If the text is \"couldn't connect you\", suspect the media "
                "layer: camera/H.264 (see launch_browser's docstring), or a renderer crash from a small "
                "/dev/shm. Check the .txt dump beside the screenshot."
            )

        # 2. Admitted?
        for text in _LEAVE_CALL_TEXTS:
            try:
                if await page.get_by_role("button", name=text).first.is_visible(timeout=1000):
                    logger.info("admitted to the meeting (found a %r control)", text)
                    await save_screenshot(page, "admitted", settings.screenshot_dir, logger)
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

    await save_screenshot(page, "admission-timeout", settings.screenshot_dir, logger)
    await dump_page_text(page, "admission-timeout", settings.screenshot_dir, logger)
    raise JoinFailure(
        f"was not admitted to the meeting within {settings.lobby_timeout_s:.0f}s -- "
        "either nobody let the bot in, or admission-detection didn't recognize the in-meeting UI. "
        "Check the .txt dump for the actual button text and update _LEAVE_CALL_TEXTS."
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

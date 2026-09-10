"""
All Microsoft Teams DOM interaction lives here -- the one module in this
bot expected to need real-world iteration against the live Teams UI.

REVISION HISTORY (each entry is a lesson from a real failed run, kept so
the same mistake isn't reintroduced):

r1 -- written blind, never having loaded a real Teams page. Reached
pre-join, clicked "Join now", then sat for the full lobby_timeout_s while
Teams was already showing "Sorry, we couldn't connect you." The camera
was ON for that attempt, because the toggle-off code probed aria-pressed
on role="button" and matched nothing.

r2 -- added fail-fast failure-text detection, a polling click helper with
one shared budget (the old nested loops multiplied a 60s timeout by every
(text, role) pair -- six wasted minutes), failure dumps, and browser
console/pageerror forwarding. Also tried to force the camera off by
WITHHOLDING the browser's camera permission. That backfired: Teams
requests audio and video in a single getUserMedia call, so denying video
denied the microphone too, every media request failed with
NotAllowedError, and Join stayed disabled.

r3 -- granted both permissions and suppressed the camera at the JS layer
instead (see _HIDE_CAMERA_INIT_SCRIPT). Media errors went away, but
faking "no webcam" pushed Teams into its no-devices consent flow: a
role="dialog" with data-tid="get-user-media-wrapper" ("Are you sure you
don't want audio or video?") appeared over the pre-join screen, and its
backdrop intercepted every click on the join button. Playwright's click()
hit-tests the target point, so an intercepted click and a missing element
produce the identical timeout. The only button that dialog offers is
"Continue without audio or video", which would likely select "Don't use
audio" and kill the speaker output this bot exists to capture -- so
clicking it is NOT an acceptable way out.

r4 (current) -- keep the fake camera DEVICE present so Teams stays in its
normal flow and never shows that dialog, and turn the camera off via the
switch instead. r3's control dump confirmed all four pre-join selectors
against the real light-meetings DOM (see the constants below), and
revealed that both media switches are real <input> elements whose state
lives in the DOM `checked` property, not aria-checked -- which is why
every aria-based probe up to r3 reported "no readable state". Also adds
click diagnostics that name the element actually sitting at a click point
when a click is intercepted, so this class of bug is identified in one
run rather than three.

This meeting link lands on Teams' *light-meetings* experience
(/light-meetings/launch?...&lightExperience=true), which goes straight to
pre-join with no app-launcher interstitial at all.
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

# --- Selectors ------------------------------------------------------------
# CONFIRMED against the real light-meetings pre-join DOM by r3's control
# dump -- these are no longer guesses. If pre-join breaks again, re-run
# and diff the new *-controls-*.json against these.

_NAME_INPUT_TID = "prejoin-display-name-input"
_JOIN_BUTTON_TID = "prejoin-join-button"
_CAMERA_SWITCH_TID = "toggle-video"
_MIC_SWITCH_TID = "toggle-mute"

# Still unconfirmed: the in-meeting leave control. r3 never got far enough
# to dump it. The "admitted" control dump will settle this.
_LEAVE_BUTTON_TIDS = ["hangup-button", "callingButtons-hangupButton"]
_LEAVE_CALL_TEXTS = ["Leave", "Leave call", "Hang up", "Leave meeting"]

# Modal dialogs known to sit over pre-join and swallow clicks. Detected
# and reported rather than dismissed -- see r3: the only exit this one
# offers is destructive to audio.
_BLOCKING_DIALOG_TIDS = ["get-user-media-wrapper"]

_NAME_INPUT_PLACEHOLDER_RE = re.compile(r"name", re.IGNORECASE)
_JOIN_NOW_TEXTS = ["Join now", "Join meeting", "Join"]

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

_BROWSER_JOIN_PARAMS = {
    "msLaunch": "0",
    "type": "meetup-join",
    "directDl": "true",
    "enableMobilePage": "true",
    "suppressPrompt": "true",
}


# --- Camera suppression (DISABLED BY DEFAULT as of r4) -------------------
# Kept, unused by default, because it is the natural thing to reach for
# again and the reason not to is non-obvious. It works exactly as
# designed -- Teams sees a mic-only machine and never negotiates video --
# but "no webcam" is precisely what triggers the get-user-media-wrapper
# consent dialog that then blocks the join button (see r3).
#
# The camera is turned off via the toggle-video switch instead, which
# leaves Teams in its normal have-devices flow.
#
# Set SUPPRESS_CAMERA_DEVICE=1 in the environment to re-enable this, e.g.
# to test whether a future Teams build stops showing that dialog.
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
      return Promise.reject(
        new DOMException('Requested device not found', 'NotFoundError')
      );
    }
    return origGum(c);
  };
})();
"""


def _suppress_camera_device_enabled() -> bool:
    return os.getenv("SUPPRESS_CAMERA_DEVICE", "").strip().lower() in ("1", "true", "yes")


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
              '[role="switch"], [role="checkbox"], [role="dialog"], ' +
              '[role="menuitem"], [data-tid]';
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
      checked: el.checked === undefined ? null : el.checked,
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

# Names the element actually sitting at a point, plus its ancestors. This
# is what turns "click timed out" into "a modal backdrop is on top of the
# button" -- the exact ambiguity that cost r3 a whole run.
_HIT_TEST_JS = """
([x, y]) => {
  const el = document.elementFromPoint(x, y);
  if (!el) return null;
  const chain = [];
  let cur = el;
  for (let i = 0; i < 5 && cur; i++) {
    chain.push({
      tag: cur.tagName.toLowerCase(),
      dataTid: cur.getAttribute('data-tid'),
      role: cur.getAttribute('role'),
      cls: (cur.className || '').toString().slice(0, 100)
    });
    cur = cur.parentElement;
  }
  return chain;
}
"""


async def dump_controls(page: Page, step_name: str, screenshot_dir: str, logger: logging.Logger) -> None:
    """
    Write every interactive element -- data-tid, ARIA state, DOM checked
    state, disabled flag, geometry, visible text -- to a JSON file next to
    the screenshots. Never raises.
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


async def _report_blocking_dialogs(page: Page, logger: logging.Logger) -> None:
    """
    Log any known click-swallowing modal that's currently up.

    Deliberately does NOT dismiss it. The get-user-media-wrapper dialog's
    only action is "Continue without audio or video", which would likely
    switch the session to "Don't use audio" -- and the speaker output is
    the entire reason this bot joins the meeting. If this fires, the fix
    is upstream: stop putting Teams into a state where it asks.
    """
    for tid in _BLOCKING_DIALOG_TIDS:
        try:
            locator = page.locator(f'[data-tid="{tid}"]').first
            if await locator.is_visible(timeout=500):
                text = (await locator.inner_text())[:200].replace("\n", " / ")
                logger.error(
                    "a modal dialog (data-tid=%r) is open over the pre-join screen and will swallow "
                    "clicks: %r. NOT auto-dismissing it -- its only action would disable audio. See r3 "
                    "in this module's docstring.",
                    tid,
                    text,
                )
        except Exception:  # noqa: BLE001
            continue


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
    - --use-fake-device-for-media-stream supplies a fake/silent mic and a
      fake camera. The silent mic is also the code-level enforcement of
      "the bot never transmits real audio into the meeting": with this
      flag it is not possible for this process to send real mic input.
      The fake CAMERA is deliberately left available -- see r4; hiding it
      is what triggered the blocking consent dialog.
    - --disable-dev-shm-usage: /dev/shm defaults to 64MB in Docker, which
      Chromium's renderer routinely exhausts. A renderer crash mid-join is
      indistinguishable from a Teams-side connection failure by the time
      you see the screenshot.
    - --enable-unsafe-swiftshader: there is no GPU under Xvfb and Chromium
      now warns that the implicit software-WebGL fallback is deprecated.
      Teams uses WebGL in the pre-join preview, so opt in explicitly.
    """
    args = [
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
        "--no-sandbox",  # containers commonly need this; the container itself is the sandbox boundary
        "--disable-dev-shm-usage",
        "--autoplay-policy=no-user-gesture-required",
        "--enable-unsafe-swiftshader",
    ]

    launch_kwargs = {
        "headless": False,
        "args": args,
        # CRITICAL (r5): Playwright's chromium DEFAULT args include
        # --mute-audio. Adding entries to `args` does not remove the
        # defaults, so without this line Chromium is muted at the browser
        # level and the PulseAudio null sink faithfully records perfect
        # digital silence -- every captured frame all-zero bytes, rms=0.
        # That is indistinguishable from a null-sink routing failure,
        # which is the wrong thing to go and debug. Playwright's own docs
        # use this exact flag as their ignore_default_args example.
        "ignore_default_args": ["--mute-audio"],
    }
    if settings.browser_channel:
        launch_kwargs["channel"] = settings.browser_channel

    browser = await playwright.chromium.launch(**launch_kwargs)
    context = await browser.new_context(permissions=["camera", "microphone"])

    if _suppress_camera_device_enabled():
        logger = logging.getLogger("teams_bot")
        logger.warning(
            "SUPPRESS_CAMERA_DEVICE is set -- hiding the camera at the JS layer. This caused Teams to "
            "show a click-blocking consent dialog in r3; expect the join button to be unclickable."
        )
        await context.add_init_script(_HIDE_CAMERA_INIT_SCRIPT)

    page = await context.new_page()

    blog = logging.getLogger("teams_bot.browser")
    page.on("console", lambda msg: blog.debug("console[%s]: %s", msg.type, msg.text))
    page.on("pageerror", lambda err: blog.warning("page error: %s", err))
    page.on("crash", lambda _: blog.error("PAGE CRASHED -- likely /dev/shm exhaustion or an OOM"))

    return browser, context, page


# --- Locator helpers ------------------------------------------------------


def _tid(page: Page, tid: str):
    return page.locator(f'[data-tid="{tid}"]').first


async def _click_with_diagnostics(
    page: Page, locator, label: str, logger: logging.Logger, timeout_ms: int = 8000
) -> bool:
    """
    Click `locator`, and if the normal click fails, say WHY before giving
    up: name whatever element is actually at the click point, then retry
    with force=True (skips the receives-events check) and finally with a
    direct DOM .click() (bypasses hit-testing entirely).

    r3 lost a full run to a click that timed out because a modal backdrop
    was on top of an otherwise perfectly enabled button -- Playwright
    reports that identically to a missing element.
    """
    try:
        await locator.click(timeout=timeout_ms)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("normal click on %s failed: %s", label, exc)

    try:
        box = await locator.bounding_box()
        if box:
            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2
            chain = await page.evaluate(_HIT_TEST_JS, [cx, cy])
            logger.warning(
                "element stack at %s's centre (%.0f, %.0f) -- the first entry is what actually "
                "receives the click: %s",
                label,
                cx,
                cy,
                json.dumps(chain),
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("hit-test diagnostic for %s failed: %s", label, exc)

    await _report_blocking_dialogs(page, logger)

    try:
        await locator.click(timeout=3000, force=True)
        logger.info("clicked %s with force=True", label)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("forced click on %s failed: %s", label, exc)

    try:
        await locator.evaluate("el => el.click()")
        logger.info("clicked %s via a direct DOM .click()", label)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("DOM click on %s failed: %s", label, exc)

    return False


async def _set_switch(page: Page, tid: str, want_on: bool, label: str, logger: logging.Logger) -> bool:
    """
    Drive one of Teams' pre-join media switches to a known state.

    These are real <input> elements with role="switch". r3's dump showed
    aria-checked is absent on them, so state lives in the DOM `checked`
    property -- every aria-based probe from r1 to r3 silently found
    nothing, which is how the camera stayed on through the first runs.
    is_checked() reads the property directly.
    """
    locator = _tid(page, tid)
    try:
        if not await locator.is_visible(timeout=2000):
            logger.warning("%s switch (data-tid=%r) is not visible", label, tid)
            return False
    except Exception:  # noqa: BLE001
        logger.warning("%s switch (data-tid=%r) not found", label, tid)
        return False

    title = await locator.get_attribute("title") or ""

    if await locator.is_disabled():
        logger.info("%s switch is disabled (title=%r) -- leaving it alone", label, title)
        return False

    try:
        is_on = await locator.is_checked()
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not read %s switch state: %s", label, exc)
        return False

    logger.info("%s switch is currently %s (title=%r)", label, "on" if is_on else "off", title)
    if is_on == want_on:
        return True

    try:
        await locator.set_checked(want_on, timeout=3000)
    except Exception:  # noqa: BLE001
        if not await _click_with_diagnostics(page, locator, f"the {label} switch", logger, timeout_ms=3000):
            return False

    try:
        now_on = await locator.is_checked()
        logger.info("%s switch is now %s", label, "on" if now_on else "off")
        return now_on == want_on
    except Exception:  # noqa: BLE001
        return False


async def _prejoin_name_input(page: Page):
    """Locate the display-name input by data-tid, falling back to placeholder."""
    locator = _tid(page, _NAME_INPUT_TID)
    try:
        if await locator.is_visible(timeout=1000):
            return locator
    except Exception:  # noqa: BLE001
        pass
    return page.get_by_placeholder(_NAME_INPUT_PLACEHOLDER_RE).first


async def _click_first_matching_text(
    page: Page, texts: list[str], total_timeout_ms: int, per_try_timeout_ms: int = 2000
) -> bool:
    """Click the first element matching any of `texts`, all candidates sharing ONE budget."""
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


# --- Join flow ------------------------------------------------------------


async def bypass_interstitial(page: Page, join_url: str, settings: BotSettings, logger: logging.Logger) -> None:
    """
    Navigate to the meeting link and land on the browser join page.

    Races the pre-join form appearing against a continue-on-browser
    control appearing, rather than doing them in sequence: the
    light-meetings experience goes straight to pre-join and has no
    interstitial at all, so sequencing wasted 20s hunting for an element
    that was never going to exist, then more on a pointless fallback
    navigation.
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


async def complete_prejoin(page: Page, settings: BotSettings, logger: logging.Logger) -> None:
    """Fill in the bot's display name, set the media switches, click Join now."""
    logger.info("filling in pre-join display name: %r", settings.bot_display_name)

    name_input = await _prejoin_name_input(page)
    try:
        await name_input.fill(settings.bot_display_name, timeout=20000)
    except PlaywrightTimeoutError as exc:
        await _diagnose(page, "prejoin-name-input-not-found", settings, logger)
        raise JoinFailure(
            "could not find the pre-join display-name input -- see the *-controls-*.json dump beside "
            "the screenshot for the real selector"
        ) from exc

    # Camera off: no video track means nothing for Teams to encode, which
    # also sidesteps H.264 entirely (Playwright's bundled Chromium before
    # v1.57 is the open-source build, with no H.264 encoder).
    if not settings.join_with_camera:
        await _set_switch(page, _CAMERA_SWITCH_TID, False, "camera", logger)

    # Mic off is cosmetic -- the fake device is silent regardless -- so a
    # failure here is logged, never fatal. Note the tid is "toggle-mute"
    # but the switch reads as mic-ON/OFF, not muted-yes/no; the logged
    # before/after state is there to catch it if that's ever inverted.
    await _set_switch(page, _MIC_SWITCH_TID, False, "microphone", logger)

    await _report_blocking_dialogs(page, logger)
    await save_screenshot(page, "prejoin-filled", settings.screenshot_dir, logger)
    # Always dumped, not just on failure: pre-join is the page whose DOM
    # keeps moving, and a successful run's dump is the reference for the
    # next time it breaks.
    await dump_controls(page, "prejoin-filled", settings.screenshot_dir, logger)

    clicked = await _click_with_diagnostics(page, _tid(page, _JOIN_BUTTON_TID), "the join button", logger)
    if not clicked:
        clicked = await _click_first_matching_text(page, _JOIN_NOW_TEXTS, total_timeout_ms=15000)

    if not clicked:
        await _diagnose(page, "prejoin-join-button-not-found", settings, logger)
        raise JoinFailure(
            "could not click the join button. The hit-test diagnostic logged just above names whatever "
            "element actually sits at the button's centre -- if it isn't the button itself, something "
            "is overlaying it."
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

        # 2. Admitted? Dump controls on success too -- this is the one
        #    screen whose selectors are still unconfirmed.
        for tid in _LEAVE_BUTTON_TIDS:
            try:
                if await _tid(page, tid).is_visible(timeout=500):
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

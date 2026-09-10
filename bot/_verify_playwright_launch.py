"""
Sandbox-verifiable dev harness proving the OTHER half of the bot's
foundations: that headed Chromium launches successfully under a virtual
display (Xvfb) with the exact launch flags browser_join.py uses
(--use-fake-ui-for-media-stream, --use-fake-device-for-media-stream), and
that logging_utils.save_screenshot works -- before any Teams-specific
automation is layered on.

Deliberately targets a local file:// page (bot/testpage/index.html)
rather than any real network destination -- the sandbox this was built in
returned HTTP 403 for a plain request to microsoft.com (outbound network
here is allowlisted/proxied), so this script can't and doesn't try to
reach Teams. It only proves the launch/screenshot mechanics, which is all
that's verifiable here -- see bot/README.md for what's handed off to a
real machine.

Usage:
    Xvfb :99 -screen 0 1920x1080x24 &
    export DISPLAY=:99
    python3 _verify_playwright_launch.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from logging_utils import configure_logging, save_screenshot  # noqa: E402

CHECK = "✓"
CROSS = "✗"

LAUNCH_ARGS = [
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
]


async def main() -> None:
    logger = configure_logging("INFO")

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print(f"{CROSS} playwright is not installed -- pip install -r requirements.txt first")
        sys.exit(1)

    display = os.environ.get("DISPLAY")
    if not display:
        print(f"{CROSS} DISPLAY is not set -- start Xvfb first, e.g.:")
        print("    Xvfb :99 -screen 0 1920x1080x24 &")
        print("    export DISPLAY=:99")
        sys.exit(1)
    print(f"Using DISPLAY={display}")

    testpage = (Path(__file__).parent / "testpage" / "index.html").resolve()
    if not testpage.exists():
        print(f"{CROSS} test page not found at {testpage}")
        sys.exit(1)

    screenshot_dir = tempfile.mkdtemp(prefix="bot-verify-screenshots-")
    print(f"Screenshots will be written to {screenshot_dir}")

    async with async_playwright() as pw:
        print(f"Launching headed Chromium with args: {LAUNCH_ARGS}")
        browser = await pw.chromium.launch(headless=False, args=LAUNCH_ARGS)
        try:
            page = await browser.new_page()
            print(f"{CHECK} Browser launched, page created")

            await page.goto(f"file://{testpage}")
            print(f"{CHECK} Navigated to local test page")

            marker_text = await page.locator("#marker").inner_text()
            if marker_text.strip() != "bot-launch-sanity-check-ok":
                print(f"{CROSS} Unexpected page content: {marker_text!r}")
                sys.exit(1)
            print(f"{CHECK} Page content verified: {marker_text!r}")

            screenshot_path = await save_screenshot(page, "sanity-check", screenshot_dir, logger)
            if not screenshot_path or not Path(screenshot_path).exists():
                print(f"{CROSS} Screenshot was not written")
                sys.exit(1)
            size = Path(screenshot_path).stat().st_size
            print(f"{CHECK} Screenshot written: {screenshot_path} ({size} bytes)")

        finally:
            await browser.close()

    print(f"{CHECK} Playwright launch mechanics verified: headed Chromium under Xvfb, "
          "fake-media launch flags, navigation, and screenshot plumbing all work.")


if __name__ == "__main__":
    asyncio.run(main())

"""
Structured logging + the per-step screenshot helper the Teams-join
automation leans on for debuggability.

browser_join.py is the one module in this bot expected to need real-world
iteration against the live Teams UI (the sandbox this was built in can't
reach teams.microsoft.com at all -- see bot/README.md). Every join-flow
step calls save_screenshot() so that when a step breaks against a real
meeting, there's a visual + logged trail to debug from instead of a blind
failure. This mirrors how real bot-builders (e.g. Recall.ai's own public
writeup on building a Teams bot) describe handling exactly this kind of
DOM fragility.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional

_step_counter = 0


def configure_logging(log_level: str) -> logging.Logger:
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    return logging.getLogger("teams_bot")


async def save_screenshot(page, step_name: str, screenshot_dir: str, logger: logging.Logger) -> Optional[str]:
    """
    Save a screenshot of `page` tagged with an incrementing step number and
    `step_name`, and log the page's current URL/title alongside it. Never
    raises -- a screenshot failure (e.g. page already closed) is logged
    and swallowed rather than crashing the join flow that's trying to
    report its own failure.
    """
    global _step_counter
    _step_counter += 1

    try:
        Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        filename = f"{_step_counter:02d}-{step_name}-{timestamp}.png"
        path = os.path.join(screenshot_dir, filename)

        await page.screenshot(path=path)

        url = page.url
        title = "<unavailable>"
        try:
            title = await page.title()
        except Exception:
            pass

        logger.info("screenshot[%s]: %s (url=%s title=%r)", step_name, path, url, title)
        return path
    except Exception as exc:
        logger.warning("failed to save screenshot for step %r: %s", step_name, exc)
        return None

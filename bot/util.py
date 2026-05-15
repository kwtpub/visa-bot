"""Small helpers shared across modules: logging, jitter, multi-selector lookup."""
from __future__ import annotations

import logging
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable

log = logging.getLogger("vfsbot")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )


# --- timing ----------------------------------------------------------------
def human_pause(lo: float = 0.4, hi: float = 1.4) -> None:
    """Short randomized pause to look less robotic between UI actions."""
    time.sleep(random.uniform(lo, hi))


def sleep_with_jitter(base_seconds: int, jitter: tuple[int, int]) -> None:
    extra = random.uniform(jitter[0], jitter[1])
    total = base_seconds + extra
    log.info("Sleeping %.0fs (%.0f base + %.0f jitter) before next check…", total, base_seconds, extra)
    time.sleep(total)


# --- selectors -------------------------------------------------------------
def _is_xpath(sel: str) -> bool:
    return sel.lstrip().startswith(("/", "(", "./"))


def first_present(sb, selectors: Iterable[str], timeout: float = 4.0):
    """Return the first selector from `selectors` that is present on the page.

    Returns the selector string (so callers can then click/type on it), or None.
    `sb` is the SeleniumBase instance (BaseCase / SB context manager).
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        for sel in selectors:
            by = "xpath" if _is_xpath(sel) else "css selector"
            try:
                if sb.is_element_present(sel, by=by):
                    return sel
            except Exception:
                continue
        time.sleep(0.25)
    return None


def first_visible(sb, selectors: Iterable[str], timeout: float = 4.0):
    """Like first_present but requires the element to be visible."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for sel in selectors:
            by = "xpath" if _is_xpath(sel) else "css selector"
            try:
                if sb.is_element_visible(sel, by=by):
                    return sel
            except Exception:
                continue
        time.sleep(0.25)
    return None


def by_of(sel: str) -> str:
    return "xpath" if _is_xpath(sel) else "css selector"


def page_has_any_text(sb, texts: Iterable[str]) -> str | None:
    """Return the first text from `texts` that appears in the page source (case-insensitive)."""
    try:
        src = sb.get_page_source().lower()
    except Exception:
        return None
    for t in texts:
        if t.lower() in src:
            return t
    return None


# --- screenshots -----------------------------------------------------------
def screenshot(sb, directory: Path, name: str, enabled: bool = True) -> Path | None:
    if not enabled:
        return None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    path = directory / f"{ts}_{safe}.png"
    try:
        sb.save_screenshot(str(path))
        log.debug("Screenshot saved: %s", path)
        return path
    except Exception as e:  # pragma: no cover
        log.debug("Could not save screenshot: %s", e)
        return None

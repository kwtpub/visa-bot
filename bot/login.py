"""Log in to the VFS Global portal: open page -> pass Turnstile -> credentials
-> login OTP -> land on the dashboard.

Raises LoginError on anything that needs operator attention (edge block, wrong
password, persistent rate-limit). The main loop decides whether to retry.
"""
from __future__ import annotations

import time

from . import selectors as S
from .otp import fill_otp_into_page, get_otp
from .util import (
    by_of,
    first_present,
    first_visible,
    human_pause,
    log,
    page_has_any_text,
    screenshot,
)


class LoginError(RuntimeError):
    """Login failed in a way the bot can't recover from on its own."""


class EdgeBlocked(LoginError):
    """Cloudflare blocked us at the edge (datacenter IP / rate limit). Need a proxy."""


class RateLimited(LoginError):
    """VFS rejected the login as too-frequent. Back off and try later."""


# --- detection helpers -----------------------------------------------------
def _check_edge_block(sb, cfg) -> None:
    hit = page_has_any_text(sb, S.EDGE_BLOCK_TEXTS)
    if hit:
        screenshot(sb, cfg.screenshot_dir, "edge_blocked", cfg.screenshots_enabled)
        raise EdgeBlocked(
            f"Cloudflare edge block detected ('{hit}'). Your IP is blocked — "
            "use a residential/mobile proxy in the applicant's country (network.proxy)."
        )


def is_queue_page(sb) -> bool:
    """True if we've been parked in the Queue-it 'waiting room'."""
    try:
        url = (sb.get_current_url() or "").lower()
    except Exception:
        url = ""
    if any(h in url for h in S.QUEUE_PAGE_HOSTS):
        return True
    return bool(page_has_any_text(sb, S.QUEUE_PAGE_TEXTS))


def wait_out_queue(sb, cfg, max_wait: int = 600) -> bool:
    """If on a queue page, wait (up to max_wait s) for it to release us.

    Queue-it auto-redirects when it's your turn, so we just poll the URL.
    Returns True if we got through (or weren't queued), False if we timed out.
    """
    if not is_queue_page(sb):
        return True
    log.warning("Parked in VFS waiting room — holding for up to %ds…", max_wait)
    screenshot(sb, cfg.screenshot_dir, "queue_page", cfg.screenshots_enabled)
    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(15)
        try:
            # nudge the page; Queue-it refreshes itself but this is harmless
            sb.refresh()
        except Exception:
            pass
        if not is_queue_page(sb):
            log.info("Released from the waiting room.")
            return True
    log.warning("Still in the waiting room after %ds — will retry next cycle.", max_wait)
    return False


def looks_logged_in(sb) -> bool:
    """Heuristic: we see a 'start booking' control or a URL past /login."""
    try:
        url = (sb.get_current_url() or "").lower()
    except Exception:
        url = ""
    if "/login" not in url and "vfsglobal.com" in url:
        # could be dashboard / schedule page
        if first_present(sb, S.START_BOOKING_BTN, timeout=2):
            return True
        # generic: a logout link usually means we're in
        try:
            if sb.is_text_visible("Logout") or sb.is_text_visible("Log Out"):
                return True
        except Exception:
            pass
    return bool(first_present(sb, S.START_BOOKING_BTN, timeout=2))


# --- the flow --------------------------------------------------------------
def _pass_turnstile(sb, cfg) -> None:
    """Best-effort Cloudflare Turnstile solve via SeleniumBase UC helpers."""
    # If there's no challenge iframe at all, nothing to do.
    if not first_present(sb, S.TURNSTILE_IFRAME, timeout=3):
        return
    log.info("Cloudflare Turnstile present — attempting UC click…")
    # SeleniumBase exposes a few helpers across versions; try them in order.
    for attempt in range(3):
        try:
            # newer name
            sb.uc_gui_click_captcha()
        except Exception:
            try:
                # older / alternative
                sb.uc_gui_handle_captcha()
            except Exception:
                try:
                    sb.uc_gui_click_cf()
                except Exception as e:
                    log.debug("UC captcha helper not available/failed: %s", e)
        human_pause(1.5, 3.0)
        if not first_present(sb, S.TURNSTILE_IFRAME, timeout=2):
            log.info("Turnstile cleared.")
            return
        # try a reconnect-open which often clears CF state
        try:
            sb.uc_open_with_reconnect(cfg.login_url, 4)
        except Exception:
            pass
        human_pause(2, 4)
    # If still here, it's not necessarily fatal — the form may still be usable,
    # or the operator can solve it when running with --show.
    if first_present(sb, S.TURNSTILE_IFRAME, timeout=2):
        log.warning("Turnstile still showing. If running with --show, solve it manually now…")
        # give a human up to 90s to click it when not headless
        if not cfg.headless:
            for _ in range(18):
                time.sleep(5)
                if not first_present(sb, S.TURNSTILE_IFRAME, timeout=1):
                    log.info("Turnstile cleared (manually).")
                    return


def perform_login(sb, cfg) -> None:
    """Drive the full login. Assumes a fresh `sb` from open_browser()."""
    url = cfg.login_url
    log.info("Opening %s", url)
    # uc_open_with_reconnect briefly disconnects the driver so CF sees a "real"
    # navigation — this is the SB-recommended way to load CF-protected pages.
    try:
        sb.uc_open_with_reconnect(url, 5)
    except Exception:
        sb.open(url)
    human_pause(2, 4)

    _check_edge_block(sb, cfg)
    # We might hit the queue even before login.
    wait_out_queue(sb, cfg)
    _check_edge_block(sb, cfg)

    _pass_turnstile(sb, cfg)
    _check_edge_block(sb, cfg)

    # --- credentials -------------------------------------------------------
    email_sel = first_visible(sb, S.LOGIN_EMAIL, timeout=15)
    if not email_sel:
        screenshot(sb, cfg.screenshot_dir, "no_login_form", cfg.screenshots_enabled)
        # maybe we're already logged in from a saved session
        if looks_logged_in(sb):
            log.info("Already appears logged in — skipping credential entry.")
            return
        raise LoginError(
            "Could not find the login form. The page layout may have changed "
            "(update bot/selectors.py LOGIN_EMAIL) or a challenge is blocking it."
        )
    pwd_sel = first_visible(sb, S.LOGIN_PASSWORD, timeout=8)
    if not pwd_sel:
        raise LoginError("Found email field but not password field — update selectors.")

    log.info("Entering credentials…")
    sb.clear(email_sel, by=by_of(email_sel))
    sb.type(email_sel, cfg.email, by=by_of(email_sel))
    human_pause()
    sb.clear(pwd_sel, by=by_of(pwd_sel))
    sb.type(pwd_sel, cfg.password, by=by_of(pwd_sel))
    human_pause()

    submit_sel = first_visible(sb, S.LOGIN_SUBMIT, timeout=8)
    if not submit_sel:
        raise LoginError("Could not find the Sign In button — update selectors.")
    sb.click(submit_sel, by=by_of(submit_sel))
    log.info("Submitted login form.")
    human_pause(3, 6)

    # Possible immediate outcomes: error banner, OTP screen, queue, dashboard.
    _check_edge_block(sb, cfg)
    wait_out_queue(sb, cfg)

    err = first_visible(sb, S.LOGIN_ERROR, timeout=4)
    if err:
        try:
            txt = sb.get_text(err, by=by_of(err))
        except Exception:
            txt = "(unreadable)"
        screenshot(sb, cfg.screenshot_dir, "login_error", cfg.screenshots_enabled)
        low = (txt or "").lower()
        if any(w in low for w in ("later", "many", "blocked", "wait")):
            raise RateLimited(f"VFS says we're rate-limited: '{txt.strip()}'. Back off ~1-2h.")
        raise LoginError(f"Login rejected: '{txt.strip()}'. Check email/password in config.yaml.")

    # --- login OTP ---------------------------------------------------------
    otp_sel = first_visible(sb, S.OTP_INPUT, timeout=8)
    if otp_sel:
        log.info("Login OTP requested.")
        # let the operator / IMAP fetch it
        code = get_otp(cfg, prompt="Enter the LOGIN OTP VFS just emailed you")
        ok = fill_otp_into_page(sb, code, S.OTP_INPUT, S.OTP_SUBMIT)
        if not ok:
            raise LoginError("Couldn't enter the OTP into the page — update OTP selectors.")
        human_pause(3, 6)
        _check_edge_block(sb, cfg)
        wait_out_queue(sb, cfg)
        # a wrong OTP usually re-shows the field with an error
        if first_visible(sb, S.OTP_INPUT, timeout=3):
            err2 = first_visible(sb, S.LOGIN_ERROR, timeout=2)
            msg = ""
            if err2:
                try:
                    msg = sb.get_text(err2, by=by_of(err2))
                except Exception:
                    pass
            screenshot(sb, cfg.screenshot_dir, "otp_error", cfg.screenshots_enabled)
            raise LoginError(f"OTP not accepted{(': ' + msg) if msg else ''}.")

    # --- confirm we're in -------------------------------------------------
    human_pause(2, 4)
    if not looks_logged_in(sb):
        # give the SPA a moment to settle / navigate
        for _ in range(6):
            time.sleep(3)
            if looks_logged_in(sb):
                break
    if not looks_logged_in(sb):
        screenshot(sb, cfg.screenshot_dir, "post_login_unknown", cfg.screenshots_enabled)
        log.warning(
            "Logged in but didn't find the expected dashboard control. "
            "Will still try to proceed — check screenshots if monitoring fails."
        )
    else:
        log.info("Login successful — on the dashboard.")

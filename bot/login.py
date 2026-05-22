"""Log in to the VFS Global portal: open page -> pass Turnstile -> credentials
-> login OTP -> land on the dashboard.

Raises LoginError on anything that needs operator attention (edge block, wrong
password, persistent rate-limit). The main loop decides whether to retry.
"""
from __future__ import annotations

import time

from . import selectors as S
from .captcha import (
    CaptchaError,
    extract_turnstile_metadata,
    extract_turnstile_sitekey,
    get_solver,
    install_turnstile_hook,
    inject_turnstile_token,
    page_url as _page_url,
)
from .otp import fill_otp_into_page, get_otp
from .session import load_browser_state, save_browser_state
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


LOGGED_IN_URL_MARKERS = (
    "/dashboard",
    "/schedule-appointment",
    "/appointment",
    "/application-detail",
)
LOGGED_IN_TITLE_MARKERS = (
    "dashboard",
    "\u043f\u0430\u043d\u0435\u043b\u044c \u0438\u043d\u0441\u0442\u0440\u0443\u043c\u0435\u043d\u0442\u043e\u0432",
)
LOGGED_IN_TEXT_MARKERS = (
    "Logout",
    "Log Out",
    "\u0412\u044b\u0439\u0442\u0438",
    "\u0417\u0430\u043f\u0438\u0441\u0430\u0442\u044c\u0441\u044f \u043d\u0430 \u043f\u0440\u0438\u0435\u043c",
    "\u0417\u0430\u043f\u0438\u0441\u0430\u0442\u044c\u0441\u044f \u043d\u0430 \u043f\u0440\u0438\u0451\u043c",
)
INVALID_SESSION_TEXT_MARKERS = (
    "session expired",
    "session is invalid",
    "invalid session",
    "\u0441\u0435\u0441\u0441\u0438\u044f \u0438\u0441\u0442\u0435\u043a\u043b\u0430",
    "\u0441\u0435\u0430\u043d\u0441 \u0438\u0441\u0442\u0451\u043a",
    "\u043d\u0435\u0434\u0435\u0439\u0441\u0442\u0432\u0438\u0442\u0435\u043b\u0435\u043d",
    "\u043d\u0435\u0434\u0435\u0439\u0441\u0442\u0432\u0438\u0442\u0435\u043b\u044c\u043d",
)
ACCESS_RESTRICTED_TEXT_MARKERS = (
    "access restricted for user id",
    "restricted for user id",
    "429001",
    "\u0434\u043e\u0441\u0442\u0443\u043f \u043e\u0433\u0440\u0430\u043d\u0438\u0447\u0435\u043d \u0434\u043b\u044f \u0438\u0434\u0435\u043d\u0442\u0438\u0444\u0438\u043a\u0430\u0442\u043e\u0440\u0430 \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044f",
    "\u043d\u0435\u043e\u0431\u044b\u0447\u043d\u0443\u044e \u0430\u043a\u0442\u0438\u0432\u043d\u043e\u0441\u0442\u044c",
    "\u0432\u0440\u0435\u043c\u0435\u043d\u043d\u043e \u043e\u0433\u0440\u0430\u043d\u0438\u0447\u0438\u043b\u0438 \u0434\u043e\u0441\u0442\u0443\u043f",
)


def _page_haystack(sb) -> str:
    try:
        url = (sb.get_current_url() or "").lower()
    except Exception:
        url = ""
    try:
        title = (sb.get_title() or "").lower()
    except Exception:
        title = ""
    try:
        body = (sb.get_text("body", by="css selector") or "").lower()
    except Exception:
        try:
            body = (sb.get_page_source() or "").lower()
        except Exception:
            body = ""
    return "\n".join((url, title, body))


def _access_restricted(sb) -> bool:
    haystack = _page_haystack(sb)
    return any(marker in haystack for marker in ACCESS_RESTRICTED_TEXT_MARKERS)


def _check_access_restricted(sb, cfg) -> None:
    if not _access_restricted(sb):
        return
    screenshot(sb, cfg.screenshot_dir, "access_restricted_429001", cfg.screenshots_enabled)
    raise RateLimited(
        "VFS restricted this user ID/account (429001) after login. "
        "Wait/unlock the account or use a different account before retrying."
    )


def _session_invalid(sb) -> bool:
    haystack = _page_haystack(sb)
    url = haystack.split("\n", 1)[0]
    if "page-not-found" in url and ("401" in haystack or "session" in haystack or "\u0441\u0435\u0441\u0441" in haystack):
        return True
    return any(marker in haystack for marker in INVALID_SESSION_TEXT_MARKERS)


def _is_login_url(url: str) -> bool:
    return "/login" in (url or "").lower()


def looks_logged_in(sb) -> bool:
    """Heuristic: dashboard URL/title, visible logout text, or a visible booking control."""
    if _access_restricted(sb):
        return False
    if _session_invalid(sb):
        return False
    try:
        url = (sb.get_current_url() or "").lower()
    except Exception:
        url = ""
    if _is_login_url(url):
        return False
    if "vfsglobal.com" in url and any(marker in url for marker in LOGGED_IN_URL_MARKERS):
        return True

    try:
        title = (sb.get_title() or "").lower()
    except Exception:
        try:
            title = (sb.driver.title or "").lower()
        except Exception:
            title = ""
    if any(marker in title for marker in LOGGED_IN_TITLE_MARKERS):
        return True

    if "vfsglobal.com" in url:
        # could be dashboard / schedule page
        if first_visible(sb, S.START_BOOKING_BTN, timeout=2):
            return True
        # generic: a logout link or booking label usually means we're in
        try:
            if any(sb.is_text_visible(text) for text in LOGGED_IN_TEXT_MARKERS):
                return True
        except Exception:
            pass
    return False


# --- the flow --------------------------------------------------------------
def _try_uc_click(sb) -> None:
    """Try every UC-mode captcha helper SeleniumBase exposes, swallowing errors."""
    for fn_name in ("uc_gui_click_captcha", "uc_gui_handle_captcha", "uc_gui_click_cf"):
        fn = getattr(sb, fn_name, None)
        if fn is None:
            continue
        try:
            fn()
            return
        except Exception as e:
            log.debug("%s failed: %s", fn_name, e)


def _turnstile_present(sb, timeout: float = 3.0) -> bool:
    """Detect Turnstile even when Cloudflare renders it in a closed shadow root."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if first_present(sb, S.TURNSTILE_IFRAME, timeout=0.25):
            return True
        try:
            src = (sb.get_page_source() or "").lower()
        except Exception:
            src = ""
        if "challenges.cloudflare.com" in src and (
            "turnstile" in src or "cf-turnstile-response" in src
        ):
            return True
        time.sleep(0.25)
    return False


def _page_looks_blank_or_error(sb) -> bool:
    """Detect blank pages, Chrome error pages, or proxy connection errors."""
    try:
        url = (sb.get_current_url() or "").lower()
    except Exception:
        url = ""
    if url.startswith("chrome-error://"):
        return True

    try:
        src = (sb.get_page_source() or "").strip().lower()
    except Exception:
        return False

    # Check for Chrome error indicators in the page source
    error_indicators = [
        "err_connection_reset",
        "err_connection_refused",
        "err_connection_timed_out",
        "err_proxy_connection_failed",
        "err_tunnel_connection_failed",
        "err_name_not_resolved",
        "net::err_",
        "dns_probe_finished",
    ]
    for indicator in error_indicators:
        if indicator in src:
            return True

    compact = "".join(src.split())
    return len(src) < 200 and compact in {
        "<html><head></head><body></body></html>",
        "<html><head></head><body></body></html>",
    }


def _open_login_page(sb, cfg, reconnect_seconds: int = 5) -> None:
    """Open the login page with retry logic for unstable proxies.

    Tries UC reconnect first, then falls back to normal open, with up to
    3 total attempts to handle proxy connection resets.
    """
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        # Try UC reconnect first (best for Cloudflare bypass)
        try:
            sb.uc_open_with_reconnect(cfg.login_url, reconnect_seconds)
        except Exception as e:
            log.debug("uc_open_with_reconnect failed (attempt %d): %s", attempt, e)
            try:
                sb.open(cfg.login_url)
            except Exception as e2:
                log.debug("normal open also failed (attempt %d): %s", attempt, e2)
                if attempt < max_retries:
                    human_pause(2.0, 4.0)
                    continue
                return

        human_pause(1.5, 3.0)

        if _page_looks_blank_or_error(sb):
            log.warning(
                "Page blank/error after load (attempt %d/%d); retrying…",
                attempt, max_retries
            )
            # Try a simple open as fallback
            try:
                sb.open(cfg.login_url)
                human_pause(2.0, 4.0)
            except Exception as e:
                log.debug("fallback open failed: %s", e)

            if _page_looks_blank_or_error(sb):
                if attempt < max_retries:
                    human_pause(3.0, 6.0)
                    continue
                log.error("Page still blank/error after %d attempts.", max_retries)
            else:
                log.info("Page loaded successfully via fallback open.")
                return
        else:
            log.debug("Page loaded successfully (attempt %d).", attempt)
            return


def _solve_with_paid_service(sb, cfg) -> bool:
    """If a paid solver is configured, fetch a token and inject it. Returns True on success."""
    install_turnstile_hook(sb)
    try:
        solver = get_solver(cfg)
    except CaptchaError as e:
        log.warning("Captcha solver disabled: %s", e)
        return False
    if solver is None:
        return False

    sitekey = extract_turnstile_sitekey(sb)
    if not sitekey:
        log.warning("Paid solver configured but couldn't find the Turnstile sitekey on the page.")
        return False
    meta = extract_turnstile_metadata(sb)
    sitekey = meta.get("sitekey") or sitekey
    url = _page_url(sb) or cfg.login_url
    log.info("Asking %s to solve Turnstile (sitekey=%s…, url=%s)",
             cfg.captcha_provider, sitekey[:12], url)
    try:
        token = solver.solve_turnstile(
            sitekey,
            url,
            action=meta.get("action") or None,
            cdata=meta.get("cData") or None,
        )
    except CaptchaError as e:
        log.error("Captcha solver failed: %s", e)
        return False

    if not inject_turnstile_token(sb, token):
        log.warning("Got a token but couldn't inject it into the page.")
        return False
    human_pause(1.0, 2.0)
    return True


def _pass_turnstile(sb, cfg) -> None:
    """Best-effort Cloudflare Turnstile solve.

    Order of attempts:
      1) SeleniumBase UC-mode auto-click (free, sometimes flaky).
      2) Paid solver (CapSolver) if configured — extract sitekey, get token,
         inject into the page.
      3) Manual fallback when running with --show: give the human ~90s to click.
    """
    install_turnstile_hook(sb)
    # Nothing on page at all -> done.
    if not _turnstile_present(sb, timeout=3):
        return

    log.info("Cloudflare Turnstile present — attempting UC click…")
    for attempt in range(2):
        _try_uc_click(sb)
        human_pause(1.5, 3.0)
        if not _turnstile_present(sb, timeout=2):
            log.info("Turnstile cleared by UC-mode.")
            return
        # try a reconnect-open which often clears CF state
        try:
            _open_login_page(sb, cfg, reconnect_seconds=4)
        except Exception:
            pass
        human_pause(2, 4)
        if not _turnstile_present(sb, timeout=2):
            log.info("Turnstile cleared after reconnect.")
            return

    # UC didn't manage it -> try the paid service.
    if cfg.captcha_enabled:
        log.info("Falling back to paid captcha service: %s", cfg.captcha_provider)
        if _solve_with_paid_service(sb, cfg):
            # Give the page a moment to validate the token
            for _ in range(6):
                if not _turnstile_present(sb, timeout=1):
                    log.info("Turnstile cleared by paid solver.")
                    return
                time.sleep(1)
            log.warning("Paid solver returned a token but Turnstile widget is still present. "
                        "The form may still accept it on submit — continuing.")
            return
        # solver returned False -> fall through to manual

    # Last resort: manual.
    if _turnstile_present(sb, timeout=2):
        log.warning("Turnstile still showing. If running with --show, solve it manually now…")
        if not cfg.headless:
            for _ in range(18):
                time.sleep(5)
                if not _turnstile_present(sb, timeout=1):
                    log.info("Turnstile cleared (manually).")
                    return



def _dismiss_cookie_banner(sb) -> None:
    """Close the OneTrust cookie consent overlay if present.

    The banner sits on top of the form and blocks clicks on inputs and buttons.
    We try to click "Accept All Cookies" (or the reject-all fallback) and then
    wait for the banner to disappear.
    """
    btn = first_visible(sb, S.COOKIE_ACCEPT_BTN, timeout=3)
    if not btn:
        return
    try:
        sb.click(btn, by=by_of(btn))
        log.debug("Cookie banner dismissed.")
    except Exception as e:
        log.debug("Could not click cookie accept button: %s", e)
        # Try to remove the banner via JS as a last resort
        try:
            sb.execute_script(
                'var b=document.getElementById("onetrust-banner-sdk");'
                'if(b)b.remove();'
            )
        except Exception:
            pass
    human_pause(0.5, 1.0)


def _wait_for_submit_enabled(sb, submit_sel: str, timeout: int = 15) -> None:
    """Wait until the submit button loses its disabled attribute.

    On the Russian portal the Sign-In button stays disabled until the
    Turnstile captcha is successfully verified.  We poll the attribute so
    the bot doesn't click a no-op button.
    """
    by = by_of(submit_sel)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            disabled = sb.get_attribute(submit_sel, "disabled", by=by)
            if disabled is None or disabled == "false":
                return  # button is enabled
        except Exception:
            return  # element gone / selector stale
        time.sleep(0.5)
    log.warning(
        "Submit button still disabled after %ds - clicking anyway (Turnstile "
        "may not have completed).", timeout
    )


def perform_login(sb, cfg) -> None:
    """Drive the full login. Assumes a fresh `sb` from open_browser()."""
    state_file = cfg.session_state_file
    if looks_logged_in(sb):
        log.info("Browser already appears logged in; skipping login form.")
        if state_file and cfg.session_export_enabled:
            save_browser_state(sb, cfg, state_file)
        return

    if state_file and cfg.session_import_enabled:
        if load_browser_state(sb, cfg, state_file):
            try:
                sb.open(cfg.login_url)
            except Exception:
                pass
            human_pause(2, 4)
            _check_edge_block(sb, cfg)
            wait_out_queue(sb, cfg)
            if looks_logged_in(sb):
                log.info("Reused saved VFS session; skipping login form.")
                if cfg.session_export_enabled:
                    save_browser_state(sb, cfg, state_file)
                return
            log.info("Saved VFS session is not logged in; falling back to normal login.")

    if cfg.manual_login_enabled:
        wait_for_manual_login(sb, cfg, state_file=state_file)
        return

    url = cfg.login_url
    log.info("Opening %s", url)
    # uc_open_with_reconnect briefly disconnects the driver so CF sees a "real"
    # navigation — this is the SB-recommended way to load CF-protected pages.
    _open_login_page(sb, cfg)
    human_pause(2, 4)

    _check_edge_block(sb, cfg)
    # We might hit the queue even before login.
    wait_out_queue(sb, cfg)
    _check_edge_block(sb, cfg)

    _pass_turnstile(sb, cfg)
    _check_edge_block(sb, cfg)

    # Dismiss cookie consent overlay (blocks form clicks if present)
    _dismiss_cookie_banner(sb)

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

    # VFS often injects the Turnstile iframe only after Angular has rendered
    # the login form. The earlier page-level check can miss it.
    human_pause(1.0, 2.0)
    _pass_turnstile(sb, cfg)
    _check_edge_block(sb, cfg)
    _dismiss_cookie_banner(sb)

    email_sel = first_visible(sb, S.LOGIN_EMAIL, timeout=15)
    if not email_sel:
        screenshot(sb, cfg.screenshot_dir, "no_login_form_after_turnstile", cfg.screenshots_enabled)
        raise LoginError("Could not find the login form after Turnstile handling.")
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
    # On some portals (especially Russian), the submit button is disabled until
    # the Turnstile captcha is verified. Wait for it to become enabled.
    _wait_for_submit_enabled(sb, submit_sel, timeout=15)
    sb.click(submit_sel, by=by_of(submit_sel))
    log.info("Submitted login form.")
    human_pause(3, 6)

    # Possible immediate outcomes: error banner, OTP screen, queue, dashboard.
    _check_edge_block(sb, cfg)
    wait_out_queue(sb, cfg)
    _check_access_restricted(sb, cfg)

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
        _check_access_restricted(sb, cfg)
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

    if state_file and cfg.session_export_enabled:
        save_browser_state(sb, cfg, state_file)


def wait_for_manual_login(sb, cfg, *, state_file=None, timeout_seconds: int | None = None) -> None:
    """Open VFS and wait until the operator finishes logging in."""
    timeout = int(timeout_seconds or cfg.manual_login_wait_seconds)
    log.info("Opening %s for manual login; waiting up to %ds.", cfg.login_url, timeout)
    _open_login_page(sb, cfg)
    human_pause(2, 4)
    _check_edge_block(sb, cfg)
    wait_out_queue(sb, cfg, max_wait=min(timeout, 600))
    _check_edge_block(sb, cfg)
    _dismiss_cookie_banner(sb)

    deadline = time.time() + timeout
    last_log = 0.0
    last_invalid_log = 0.0
    while time.time() < deadline:
        _check_access_restricted(sb, cfg)
        if _session_invalid(sb):
            now = time.time()
            if now - last_invalid_log > 30:
                log.warning("VFS reports an expired/invalid session; reopening login page.")
                last_invalid_log = now
            _open_login_page(sb, cfg)
            human_pause(2, 4)
            continue
        if looks_logged_in(sb):
            log.info("Manual VFS login detected.")
            if state_file and cfg.session_export_enabled:
                save_browser_state(sb, cfg, state_file)
            return

        now = time.time()
        if now - last_log > 30:
            remaining = max(0, int(deadline - now))
            log.info("Waiting for manual VFS login... %ds left.", remaining)
            last_log = now
        time.sleep(5)

    screenshot(sb, cfg.screenshot_dir, "manual_login_timeout", cfg.screenshots_enabled)
    raise LoginError(f"Timed out waiting {timeout}s for manual VFS login.")

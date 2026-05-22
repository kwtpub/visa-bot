"""Smoke test: open the Slovenia login page and try to log in.

Run with:
    python -m tests.test_smoke_svn

This script:
  1. Loads config.yaml (portal should point to rus/ru/svn)
  2. Opens the VFS login page via UC mode
  3. Handles Turnstile captcha (paid solver via CapSolver)
  4. Enters credentials
  5. Takes screenshots at each stage
"""
from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

# Ensure the project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import load_config
from bot.browser import open_browser
from bot.util import log, setup_logging, screenshot


def _get_title(sb) -> str:
    """Get page title safely across SeleniumBase versions."""
    try:
        return sb.get_title() or ""
    except Exception:
        try:
            return sb.driver.title or ""
        except Exception:
            return ""


def main() -> int:
    cfg = load_config()
    setup_logging("DEBUG")

    log.info("=== SMOKE TEST: Slovenia (rus/ru/svn) ===")
    log.info("Login URL: %s", cfg.login_url)
    log.info("Proxy: %s", "SET" if cfg.proxy else "NOT SET")
    log.info("Captcha: provider=%s  enabled=%s", cfg.captcha_provider, cfg.captcha_enabled)

    if cfg.captcha_enabled:
        from bot.captcha import CapSolver
        try:
            solver = CapSolver(cfg.captcha_api_key, cfg.captcha_timeout)
            bal = solver.balance()
            log.info("CapSolver balance: $%.2f", bal or 0)
        except Exception as e:
            log.warning("Could not check CapSolver balance: %s", e)

    try:
        with open_browser(cfg) as sb:
            # --- Step 1: Open login page ---
            log.info("--- Step 1: Opening login page ---")
            from bot.login import _open_login_page, _check_edge_block, _pass_turnstile
            from bot.login import _dismiss_cookie_banner, _wait_for_submit_enabled
            from bot.login import wait_out_queue, looks_logged_in
            from bot import selectors as S
            from bot.util import first_visible, first_present, by_of, human_pause

            _open_login_page(sb, cfg)
            human_pause(2, 4)
            screenshot(sb, cfg.screenshot_dir, "step1_page_loaded", True)

            url = sb.get_current_url() or ""
            title = _get_title(sb)
            log.info("Current URL: %s", url)
            log.info("Page title: %s", title)

            # --- Step 2: Check for edge block ---
            log.info("--- Step 2: Checking for edge block ---")
            try:
                _check_edge_block(sb, cfg)
                log.info("No edge block detected.")
            except Exception as e:
                log.error("EDGE BLOCK: %s", e)
                screenshot(sb, cfg.screenshot_dir, "step2_edge_block", True)
                return 1

            # --- Step 3: Wait out queue if any ---
            log.info("--- Step 3: Checking for queue page ---")
            wait_out_queue(sb, cfg)

            # --- Step 4: Handle Turnstile ---
            log.info("--- Step 4: Handling Turnstile captcha ---")
            _pass_turnstile(sb, cfg)
            screenshot(sb, cfg.screenshot_dir, "step4_after_turnstile", True)

            # --- Step 5: Dismiss cookie banner ---
            log.info("--- Step 5: Dismissing cookie banner ---")
            _dismiss_cookie_banner(sb)
            screenshot(sb, cfg.screenshot_dir, "step5_after_cookies", True)

            # --- Step 6: Find login form ---
            log.info("--- Step 6: Looking for login form ---")
            email_sel = first_visible(sb, S.LOGIN_EMAIL, timeout=15)
            if email_sel:
                log.info("Email field FOUND: %s", email_sel)
            else:
                log.error("Email field NOT FOUND")
                screenshot(sb, cfg.screenshot_dir, "step6_no_email_field", True)
                if looks_logged_in(sb):
                    log.info("Already logged in!")
                    return 0
                return 1

            pwd_sel = first_visible(sb, S.LOGIN_PASSWORD, timeout=8)
            if pwd_sel:
                log.info("Password field FOUND: %s", pwd_sel)
            else:
                log.error("Password field NOT FOUND")
                return 1

            # VFS can inject Turnstile only after the Angular login form exists.
            log.info("--- Step 6b: Rechecking Turnstile after form render ---")
            human_pause(1.0, 2.0)
            _pass_turnstile(sb, cfg)
            _dismiss_cookie_banner(sb)
            email_sel = first_visible(sb, S.LOGIN_EMAIL, timeout=15)
            pwd_sel = first_visible(sb, S.LOGIN_PASSWORD, timeout=8)
            if not email_sel or not pwd_sel:
                log.error("Login fields disappeared after Turnstile handling")
                screenshot(sb, cfg.screenshot_dir, "step6b_no_login_fields", True)
                return 1

            # --- Step 7: Enter credentials ---
            log.info("--- Step 7: Entering credentials ---")
            sb.clear(email_sel, by=by_of(email_sel))
            sb.type(email_sel, cfg.email, by=by_of(email_sel))
            human_pause()
            sb.clear(pwd_sel, by=by_of(pwd_sel))
            sb.type(pwd_sel, cfg.password, by=by_of(pwd_sel))
            human_pause()
            screenshot(sb, cfg.screenshot_dir, "step7_credentials_entered", True)

            # --- Step 8: Find and click submit ---
            log.info("--- Step 8: Looking for submit button ---")
            submit_sel = first_visible(sb, S.LOGIN_SUBMIT, timeout=8)
            if submit_sel:
                log.info("Submit button FOUND: %s", submit_sel)
                _wait_for_submit_enabled(sb, submit_sel, timeout=30)
                sb.click(submit_sel, by=by_of(submit_sel))
                log.info("Login form SUBMITTED")
            else:
                log.error("Submit button NOT FOUND")
                screenshot(sb, cfg.screenshot_dir, "step8_no_submit", True)
                return 1

            human_pause(3, 6)
            screenshot(sb, cfg.screenshot_dir, "step8_after_submit", True)

            # --- Step 9: Check result ---
            log.info("--- Step 9: Checking login result ---")
            url = sb.get_current_url() or ""
            title = _get_title(sb)
            log.info("Current URL: %s", url)
            log.info("Page title: %s", title)

            # Check for error messages
            err_sel = first_visible(sb, S.LOGIN_ERROR, timeout=4)
            if err_sel:
                try:
                    txt = sb.get_text(err_sel, by=by_of(err_sel))
                except Exception:
                    txt = "(unreadable)"
                log.error("LOGIN ERROR: %s", txt)
                screenshot(sb, cfg.screenshot_dir, "step9_login_error", True)
                return 1

            # Check for OTP
            otp_sel = first_visible(sb, S.OTP_INPUT, timeout=8)
            if otp_sel:
                log.info("OTP INPUT detected - login credentials accepted!")
                log.info("OTP mode: %s", cfg.otp_mode)
                screenshot(sb, cfg.screenshot_dir, "step9_otp_requested", True)
                log.info("=== SMOKE TEST PASSED: OTP stage reached ===")
                return 0

            # Check if we're logged in
            if looks_logged_in(sb):
                log.info("=== SMOKE TEST PASSED: Logged in successfully! ===")
                screenshot(sb, cfg.screenshot_dir, "step9_logged_in", True)
                return 0

            # Wait a bit more and check again
            for i in range(6):
                time.sleep(3)
                if looks_logged_in(sb):
                    log.info("=== SMOKE TEST PASSED: Logged in (after wait) ===")
                    screenshot(sb, cfg.screenshot_dir, "step9_logged_in_delayed", True)
                    return 0
                otp_sel = first_visible(sb, S.OTP_INPUT, timeout=2)
                if otp_sel:
                    log.info("=== SMOKE TEST PASSED: OTP stage reached (after wait) ===")
                    screenshot(sb, cfg.screenshot_dir, "step9_otp_delayed", True)
                    return 0

            log.warning("Login result unclear - not obviously in or out")
            screenshot(sb, cfg.screenshot_dir, "step9_unclear", True)
            return 1

    except KeyboardInterrupt:
        log.info("Interrupted by user")
        return 130
    except Exception as e:
        log.exception("Smoke test failed: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())

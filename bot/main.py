"""Entry point: glue login -> monitor -> (book) into a polite, resilient loop.

    python -m bot.main                # monitor + auto-book per config
    python -m bot.main --no-book      # never book, just notify
    python -m bot.main --once         # one check then exit (cron-friendly)
    python -m bot.main --inspect      # print the dropdown options for config.yaml
    python -m bot.main --show         # force a visible browser window
    python -m bot.main --config path  # use a different config file

Design notes:
  * One browser session is reused across checks; we only re-login if the
    session looks dead `relogin_after_failures` times in a row.
  * Every wait gets random jitter so the cadence isn't robotic.
  * Anything that needs a human (edge block, wrong password, rate limit) stops
    the run with a clear message instead of hammering.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .accounts import load_account_pool
from .booking import BookingError, attempt_booking
from .browser import open_browser
from .config import load_config
from .login import (
    EdgeBlocked,
    LoginError,
    RateLimited,
    auto_login,
    looks_logged_in,
    perform_login,
)
from .monitor import MonitorError, check_availability, inspect_options
from .notify import Notifier
from .proxycheck import ProxyDead, precheck_proxy
from .registration import (
    RegistrationAlreadyExists,
    RegistrationError,
    check_registration_form_ready,
    register_account,
)
from .util import log, screenshot, setup_logging, sleep_with_jitter


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="bot.main", description="VFS Global appointment monitor + auto-booker")
    p.add_argument("--config", type=Path, default=None, help="path to config.yaml")
    p.add_argument("--no-book", action="store_true", help="never attempt to book, only notify")
    p.add_argument("--book", action="store_true", help="force auto-book on (overrides config)")
    p.add_argument("--dry-run", action="store_true", help="walk the booking flow but stop before final Confirm")
    p.add_argument("--once", action="store_true", help="run a single check and exit")
    p.add_argument("--inspect", action="store_true", help="log in and print the dropdown options, then exit")
    p.add_argument("--register-accounts", type=int, default=0, metavar="N", help="register up to N fresh accounts from account_pool and exit")
    p.add_argument("--check-registration-form", action="store_true", help="open VFS registration form and verify selectors without submitting")
    p.add_argument("--show", action="store_true", help="force a visible (non-headless) browser")
    p.add_argument("--cookies", type=Path, default=None, help="browser-state JSON to import/export for session reuse")
    p.add_argument("--save-cookies", type=Path, default=None, help="open VFS and wait for manual login, then save browser state")
    login_group = p.add_mutually_exclusive_group()
    login_group.add_argument("--auto-login", action="store_true", help="force account.email/password login")
    login_group.add_argument("--manual-login", action="store_true", help="wait for manual login before monitoring/booking")
    return p.parse_args(argv)


def _effective_auto_book(cfg, args) -> bool:
    if args.no_book:
        return False
    if args.dry_run or cfg.auto_book_dry_run:
        return True
    if args.book:
        return True
    return cfg.auto_book


def _register_accounts_from_pool(cfg, account_pool, limit: int) -> int:
    ready = 0
    attempted = 0
    while attempted < limit:
        if not account_pool.select_for_registration():
            log.info("No fresh account left to register.")
            break
        account_pool.apply_current()
        attempted += 1
        try:
            with open_browser(cfg) as sb:
                register_account(sb, cfg, account_pool.current)
                log.info("Verifying registered VFS account by logging in.")
                perform_login(sb, cfg)
                if not looks_logged_in(sb):
                    raise LoginError("Registered account login verification did not reach the dashboard.")
        except RegistrationAlreadyExists as e:
            log.warning("Registration skipped (already exists): %s", e)
            account_pool.mark_needs_activation(str(e))
        except RegistrationError as e:
            log.error("Registration failed: %s", e)
            account_pool.mark_registration_failure(str(e))
        except RateLimited as e:
            log.error("Registered account login verification failed: %s", e)
            reason = str(e)
            if _looks_account_banned(reason):
                account_pool.mark_banned(reason)
            else:
                account_pool.mark_failure(reason, restricted=True)
        except EdgeBlocked as e:
            log.error("Registration blocked by VFS edge protection: %s", e)
            account_pool.mark_needs_registration(str(e))
            break
        except LoginError as e:
            log.error("Registered account login verification failed: %s", e)
            reason = str(e)
            if "not registered" in reason.lower() or "не зарегистр" in reason.lower():
                account_pool.mark_needs_registration(reason)
            elif _looks_login_verification_transient(reason):
                account_pool.mark_failure(reason)
            else:
                account_pool.mark_registered()
        except Exception as e:  # pragma: no cover - live VFS/browser failures
            log.exception("Unexpected registration error: %s", e)
            account_pool.mark_registration_failure(str(e))
        else:
            account_pool.mark_success()
            ready += 1

    log.info("Account registration finished: %d/%d account(s) ready.", ready, attempted)
    return ready


def _looks_account_banned(reason: str) -> bool:
    text = str(reason or "").lower()
    return any(
        marker in text
        for marker in (
            "429201",
            "account blocked",
            "аккаунт заблокирован",
            "temporarily blocked this account/user",
        )
    )


def _looks_login_verification_transient(reason: str) -> bool:
    text = str(reason or "").lower()
    return any(
        marker in text
        for marker in (
            "did not reach the dashboard",
            "session expired",
            "session is invalid",
            "401",
            "сессия истекла",
            "недействительна",
        )
    )


def _select_pool_account_for_login(cfg, account_pool) -> bool:
    if not account_pool:
        return True
    registered_only = cfg.registration_auto_register
    if not registered_only and account_pool.select_next(registered_only=True):
        account_pool.apply_current()
        return True
    if not account_pool.select_next(registered_only=registered_only):
        if registered_only:
            log.error("No registered account available in account pool.")
        else:
            log.error("No available account in account pool.")
        return False
    account_pool.apply_current()
    return True


def run(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    cfg = load_config(args.config)
    setup_logging(cfg.log_level)

    if args.show:
        cfg.raw.setdefault("network", {})["headless"] = False
    if args.cookies:
        state_file = args.cookies if args.cookies.is_absolute() else Path.cwd() / args.cookies
        cfg.raw.setdefault("session", {})["cookies_file"] = str(state_file)
    if args.dry_run:
        cfg.raw.setdefault("behaviour", {})["auto_book_dry_run"] = True
    if args.auto_login:
        cfg.raw.setdefault("session", {})["manual_login"] = False
    elif args.manual_login:
        cfg.raw.setdefault("session", {})["manual_login"] = True

    try:
        account_pool = load_account_pool(cfg)
    except Exception as e:
        log.error("Account pool setup failed: %s", e)
        return 1

    auto_book = _effective_auto_book(cfg, args)
    cfg.raw.setdefault("behaviour", {})["auto_book"] = auto_book
    login = auto_login if args.auto_login else perform_login
    notifier = Notifier(cfg)

    log.info(
        "=== VFS bot ===  portal=%s  auto_book=%s  dry_run=%s  manual_login=%s  inspect=%s  once=%s",
        cfg.login_url,
        auto_book,
        cfg.auto_book_dry_run,
        cfg.manual_login_enabled,
        args.inspect,
        args.once,
    )

    try:
        precheck_proxy(cfg)
    except ProxyDead as e:
        # The precheck uses `requests`+PySocks, whose SOCKS5 path is unreliable
        # (intermittent ConnectTimeout) even when the proxy is fine — Chrome's
        # own SOCKS5 client reaches the site regardless. So by default a failed
        # precheck only WARNS and lets the real browser try. Set
        # network.proxy_precheck_fatal: true to abort instead.
        if getattr(cfg, "proxy_precheck_fatal", False):
            log.error("%s", e)
            notifier.error("proxy precheck", e)
            return 1
        log.warning(
            "%s — precheck is advisory (proxy_precheck_fatal=false); "
            "continuing and letting the browser use the proxy directly.", e,
        )

    if args.check_registration_form:
        try:
            with open_browser(cfg) as sb:
                state = check_registration_form_ready(sb, cfg)
            log.info(
                "Registration form check: ready=%s reason=%s url=%s",
                state.get("ready"),
                state.get("reason"),
                state.get("url"),
            )
            if state.get("missing"):
                log.error("Missing registration controls: %s", ", ".join(state["missing"]))
            if state.get("screenshot"):
                log.info("Registration check screenshot: %s", state["screenshot"])
            return 0 if state.get("ready") else 1
        except Exception as e:
            log.exception("Registration form check failed: %s", e)
            return 1

    if args.register_accounts:
        if not account_pool:
            log.error("--register-accounts requires account_pool.enabled=true.")
            return 1
        return 0 if _register_accounts_from_pool(cfg, account_pool, args.register_accounts) else 1

    if account_pool and cfg.registration_auto_register:
        _register_accounts_from_pool(cfg, account_pool, cfg.registration_max_per_run)

    if not _select_pool_account_for_login(cfg, account_pool):
        return 1

    if args.save_cookies:
        from .login import wait_for_manual_login

        cfg.raw.setdefault("network", {})["headless"] = False
        cfg.raw.setdefault("session", {})["export_cookies"] = True
        state_file = args.save_cookies
        if not state_file.is_absolute():
            state_file = Path.cwd() / state_file
        try:
            with open_browser(cfg) as sb:
                wait_for_manual_login(sb, cfg, state_file=state_file)
                return 0
        except Exception as e:
            log.exception("Could not save cookies: %s", e)
            return 1

    # --- inspect mode: one shot ------------------------------------------
    if args.inspect:
        try:
            with open_browser(cfg) as sb:
                login(sb, cfg)
                inspect_options(sb, cfg)
            return 0
        except LoginError as e:
            log.error("Login failed during --inspect: %s", e)
            return 1
        except Exception as e:  # pragma: no cover
            log.exception("Unexpected error during --inspect: %s", e)
            return 1

    notifier.started(cfg)
    bookings_done = 0
    consecutive_failures = 0
    check_no = 0

    def rotate_account_after_error(
        error: Exception,
        *,
        restricted: bool = False,
        bad_credentials: bool = False,
    ) -> bool:
        if not account_pool:
            return False
        return account_pool.rotate_after_failure(
            cfg,
            str(error),
            restricted=restricted,
            bad_credentials=bad_credentials,
            registered_only=cfg.registration_auto_register,
        )

    # We keep one browser open and reuse it; reopen on hard failures.
    while True:
        try:
            with open_browser(cfg) as sb:
                # log in once for this browser session
                try:
                    login(sb, cfg)
                except RateLimited as e:
                    notifier.error("login (rate-limited)", e)
                    log.error("%s", e)
                    if args.once:
                        return 1
                    if account_pool:
                        if rotate_account_after_error(e, restricted=True):
                            consecutive_failures = 0
                            continue
                        return 1
                    # long back-off before trying a whole new session
                    log.info("Backing off 90 minutes due to rate limit…")
                    time.sleep(90 * 60)
                    continue
                except EdgeBlocked as e:
                    notifier.error("login (edge-blocked)", e)
                    log.error("%s", e)
                    log.error("Stopping: this needs a residential proxy. Fix network.proxy and rerun.")
                    return 1
                except LoginError as e:
                    notifier.error("login", e)
                    log.error("%s", e)
                    text = str(e).lower()
                    bad_credentials = "email/password" in text or "login rejected" in text
                    if args.once:
                        return 1
                    if account_pool:
                        if rotate_account_after_error(e, bad_credentials=bad_credentials):
                            consecutive_failures = 0
                            continue
                        return 1
                    # could be transient (challenge) — retry after a normal wait
                    sleep_with_jitter(cfg.check_interval, cfg.jitter)
                    continue
                else:
                    if account_pool:
                        account_pool.mark_success()

                # --- check / book loop on this session ----------------------
                while True:
                    check_no += 1
                    log.info("--- check #%d ---", check_no)
                    # session sanity
                    if not looks_logged_in(sb):
                        consecutive_failures += 1
                        log.warning("Session doesn't look logged in (failure %d/%d).",
                                    consecutive_failures, cfg.relogin_after_failures)
                        if consecutive_failures >= cfg.relogin_after_failures:
                            log.info("Reopening browser & re-logging in…")
                            consecutive_failures = 0
                            break  # exits inner loop -> with-block closes -> new session
                        # otherwise try a quick re-login in the same browser
                        try:
                            login(sb, cfg)
                        except LoginError as e:
                            log.warning("Quick re-login failed: %s", e)
                            if args.once:
                                return 1
                            if account_pool:
                                if rotate_account_after_error(e):
                                    consecutive_failures = 0
                                    break
                                return 1
                            sleep_with_jitter(cfg.check_interval, cfg.jitter)
                            continue

                    try:
                        avail = check_availability(sb, cfg)
                    except MonitorError as e:
                        consecutive_failures += 1
                        log.error("Monitoring error: %s", e)
                        notifier.error("monitoring", e)
                        if args.once:
                            return 1
                        if consecutive_failures >= cfg.relogin_after_failures:
                            consecutive_failures = 0
                            break
                        sleep_with_jitter(cfg.check_interval, cfg.jitter)
                        continue

                    consecutive_failures = 0  # a clean check resets the counter

                    if avail.available:
                        log.info("AVAILABLE: %s", avail.note)
                        notifier.slots_found(avail.dates, avail.note)
                        if auto_book:
                            try:
                                result = attempt_booking(sb, cfg, avail)
                            except BookingError as e:
                                log.error("Booking error: %s", e)
                                notifier.error("booking", e)
                                screenshot(sb, cfg.screenshot_dir, "booking_exception", cfg.screenshots_enabled)
                                result = None
                            if result and result.booked:
                                bookings_done += 1
                                notifier.booked(result)
                                # attach the confirmation screenshot if we have one
                                shots = sorted(cfg.screenshot_dir.glob("*BOOKING_CONFIRMED*.png"))
                                if shots:
                                    notifier.send_photo(shots[-1], caption="Confirmation page")
                                if cfg.stop_after_bookings and bookings_done >= cfg.stop_after_bookings:
                                    log.info("Reached stop_after_bookings=%d — exiting.", cfg.stop_after_bookings)
                                    return 0
                            elif result and result.dry_run:
                                log.info("Dry-run completed: %s", result.note)
                                notifier.send(
                                    "<b>Dry-run reached final confirmation step.</b>\n"
                                    f"Date attempted: {result.date or '(unknown)'}\n"
                                    f"{result.note}"
                                )
                                return 0
                            elif result is not None:
                                notifier.booking_failed(result)
                            # after a booking attempt, the SPA state is messy — start a
                            # fresh session next round
                            break
                        else:
                            log.info("auto_book is off — not booking. You do it manually.")
                            if args.once:
                                return 0
                    else:
                        log.info("No availability: %s", avail.note)

                    # heartbeat
                    if (
                        cfg.telegram_heartbeat_every
                        and check_no % cfg.telegram_heartbeat_every == 0
                    ):
                        notifier.heartbeat(check_no, avail.note)

                    if args.once:
                        return 0

                    sleep_with_jitter(cfg.check_interval, cfg.jitter)
                # inner loop broke -> close browser, loop reopens a new one

        except KeyboardInterrupt:
            log.info("Interrupted by user — bye.")
            return 130
        except Exception as e:  # pragma: no cover - last-resort safety net
            log.exception("Unexpected top-level error: %s", e)
            notifier.error("main loop", e)
            if args.once:
                return 1
            # don't tight-loop on a persistent crash
            log.info("Recovering in 60s…")
            time.sleep(60)
            continue


if __name__ == "__main__":
    raise SystemExit(run())

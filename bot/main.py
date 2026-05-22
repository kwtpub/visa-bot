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

from .booking import BookingError, attempt_booking
from .browser import open_browser
from .config import load_config
from .login import EdgeBlocked, LoginError, RateLimited, looks_logged_in, perform_login
from .monitor import MonitorError, check_availability, inspect_options
from .notify import Notifier
from .proxycheck import ProxyDead, precheck_proxy
from .util import log, screenshot, setup_logging, sleep_with_jitter


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="bot.main", description="VFS Global appointment monitor + auto-booker")
    p.add_argument("--config", type=Path, default=None, help="path to config.yaml")
    p.add_argument("--no-book", action="store_true", help="never attempt to book, only notify")
    p.add_argument("--book", action="store_true", help="force auto-book on (overrides config)")
    p.add_argument("--dry-run", action="store_true", help="walk the booking flow but stop before final Confirm")
    p.add_argument("--once", action="store_true", help="run a single check and exit")
    p.add_argument("--inspect", action="store_true", help="log in and print the dropdown options, then exit")
    p.add_argument("--show", action="store_true", help="force a visible (non-headless) browser")
    p.add_argument("--cookies", type=Path, default=None, help="browser-state JSON to import/export for session reuse")
    p.add_argument("--save-cookies", type=Path, default=None, help="open VFS and wait for manual login, then save browser state")
    return p.parse_args(argv)


def _effective_auto_book(cfg, args) -> bool:
    if args.no_book:
        return False
    if args.dry_run or cfg.auto_book_dry_run:
        return True
    if args.book:
        return True
    return cfg.auto_book


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

    auto_book = _effective_auto_book(cfg, args)
    cfg.raw.setdefault("behaviour", {})["auto_book"] = auto_book
    notifier = Notifier(cfg)

    log.info("=== VFS bot ===  portal=%s  auto_book=%s  dry_run=%s  inspect=%s  once=%s",
             cfg.login_url, auto_book, cfg.auto_book_dry_run, args.inspect, args.once)

    try:
        precheck_proxy(cfg)
    except ProxyDead as e:
        log.error("%s", e)
        notifier.error("proxy precheck", e)
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
                perform_login(sb, cfg)
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

    # We keep one browser open and reuse it; reopen on hard failures.
    while True:
        try:
            with open_browser(cfg) as sb:
                # log in once for this browser session
                try:
                    perform_login(sb, cfg)
                except RateLimited as e:
                    notifier.error("login (rate-limited)", e)
                    log.error("%s", e)
                    if args.once:
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
                    if args.once:
                        return 1
                    # could be transient (challenge) — retry after a normal wait
                    sleep_with_jitter(cfg.check_interval, cfg.jitter)
                    continue

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
                            perform_login(sb, cfg)
                        except LoginError as e:
                            log.warning("Quick re-login failed: %s", e)
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

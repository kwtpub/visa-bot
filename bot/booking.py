"""Attempt to actually book a slot, continuing from a calendar page where
monitor.check_availability() found free dates.

Flow: pick a date -> pick a time slot -> fill applicant details (best effort)
-> review -> handle the confirmation OTP if asked -> confirm -> screenshot the
confirmation page. Returns a BookingResult.

This is the most fragile part of any VFS bot because the applicant-details and
review pages differ a lot between portals. The bot fills the fields it
recognises (bot/selectors.py APPLICANT_FIELDS); anything it can't fill it logs
and leaves for you — if running with --show you can complete it by hand and the
bot will still try to click "Confirm".
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

from . import selectors as S
from .login import wait_out_queue
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


@dataclass
class BookingResult:
    booked: bool = False
    reference: str = ""
    date: str = ""
    note: str = ""


class BookingError(RuntimeError):
    pass


# --- date / time pickers ---------------------------------------------------
def _pick_date(sb, cfg, preferred: list[str]) -> str:
    """Click the first selectable calendar day; prefer one from `preferred`.

    Returns the date string actually clicked (best-effort).
    """
    if not first_present(sb, S.CALENDAR_ROOT, timeout=8):
        raise BookingError("Expected a calendar to pick a date from, but none found.")

    preferred_days = set()
    preferred_ym = set()
    for d in preferred:
        m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", d)
        if m:
            preferred_days.add(int(m.group(3)))
            preferred_ym.add((int(m.group(1)), int(m.group(2))))

    # Walk months looking for an enabled day, preferring the configured window.
    from .monitor import _calendar_label_month_year  # reuse the header parser

    for _ in range(4):
        my = _calendar_label_month_year(sb)
        enabled_sel = first_present(sb, S.CALENDAR_DAY_AVAILABLE, timeout=2)
        if enabled_sel:
            try:
                els = sb.find_elements(enabled_sel, by=by_of(enabled_sel))
            except Exception:
                els = []
            # First pass: a preferred day in a preferred month.
            target = None
            for el in els:
                txt = (el.text or el.get_attribute("aria-label") or "").strip()
                dn = re.search(r"\b(\d{1,2})\b", txt)
                if not dn:
                    continue
                day = int(dn.group(1))
                if my and (my[1], my[0]) in preferred_ym and day in preferred_days:
                    target = (el, my and f"{my[1]:04d}-{my[0]:02d}-{day:02d}" or txt)
                    break
            # Second pass: any enabled day.
            if target is None and els:
                el = els[0]
                txt = (el.text or el.get_attribute("aria-label") or "").strip()
                dn = re.search(r"\b(\d{1,2})\b", txt)
                day = int(dn.group(1)) if dn else 0
                target = (el, my and day and f"{my[1]:04d}-{my[0]:02d}-{day:02d}" or txt)
            if target:
                el, label = target
                try:
                    el.click()
                except Exception:
                    sb.execute_script("arguments[0].click();", el)
                human_pause(1, 2)
                log.info("Picked date: %s", label)
                return label
        # next month
        nxt = first_visible(sb, S.CALENDAR_NEXT_MONTH, timeout=2)
        if not nxt:
            break
        sb.click(nxt, by=by_of(nxt))
        human_pause(0.8, 1.6)
    raise BookingError("Couldn't click any enabled calendar day (slot may have just been taken).")


def _pick_time_slot(sb, cfg) -> bool:
    """If a list of time slots appears, click the first available one."""
    sel = first_visible(sb, S.TIME_SLOT_AVAILABLE, timeout=8)
    if not sel:
        log.debug("No separate time-slot list — continuing.")
        return False
    sb.click(sel, by=by_of(sel))
    human_pause(0.8, 1.6)
    log.info("Picked a time slot.")
    return True


# --- applicant details -----------------------------------------------------
def _fill_applicant(sb, cfg) -> None:
    """Best-effort fill of the applicant-details form from config.applicants[0]."""
    applicants = cfg.applicants
    if not applicants:
        log.warning("No applicant data in config — relying on portal pre-fill.")
        return
    a = applicants[0]
    filled = []
    for key, sels in S.APPLICANT_FIELDS.items():
        val = a.get(key)
        if not val:
            continue
        sel = first_visible(sb, sels, timeout=2)
        if not sel:
            continue
        try:
            sb.clear(sel, by=by_of(sel))
            sb.type(sel, str(val), by=by_of(sel))
            filled.append(key)
            human_pause(0.2, 0.6)
        except Exception as e:
            log.debug("Couldn't fill %s: %s", key, e)
    # gender / nationality are usually selects
    for sel_list, val in (
        (S.APPLICANT_GENDER_SELECT, a.get("gender")),
        (S.APPLICANT_NATIONALITY_SELECT, a.get("nationality")),
    ):
        if not val:
            continue
        trig = first_visible(sb, sel_list, timeout=2)
        if not trig:
            continue
        try:
            tag = sb.get_attribute(trig, "tagName", by=by_of(trig)) or ""
            if tag.lower() == "select":
                sb.select_option_by_text(trig, str(val), by=by_of(trig))
            else:  # mat-select
                sb.click(trig, by=by_of(trig))
                human_pause(0.4, 0.9)
                xp = f'//mat-option//span[contains(normalize-space(), "{val}")]'
                if sb.is_element_present(xp, by="xpath"):
                    sb.click(xp, by="xpath")
            filled.append("gender/nationality")
            human_pause(0.2, 0.6)
        except Exception as e:
            log.debug("Couldn't set select %r: %s", val, e)
    if filled:
        log.info("Filled applicant fields: %s", ", ".join(sorted(set(filled))))
    else:
        log.warning("Couldn't fill any applicant fields — markup likely changed (APPLICANT_FIELDS).")


# --- confirm ---------------------------------------------------------------
def _extract_reference(sb) -> str:
    """Try to scrape a booking reference / confirmation number off the page."""
    try:
        src = sb.get_page_source()
    except Exception:
        return ""
    for pat in (
        r"[Rr]eference(?:\s*[Nn]umber)?\s*[:#]?\s*([A-Z0-9\-]{5,})",
        r"[Cc]onfirmation\s*(?:[Nn]umber|[Cc]ode)?\s*[:#]?\s*([A-Z0-9\-]{5,})",
        r"[Aa]ppointment\s*ID\s*[:#]?\s*([A-Z0-9\-]{5,})",
    ):
        m = re.search(pat, src)
        if m:
            return m.group(1)
    return ""


def _click_through_continues(sb, cfg, max_steps: int = 6) -> None:
    """Click 'Continue/Next/Proceed' a few times to walk multi-step forms.

    Stops when it sees a Confirm/Pay button or a success page.
    """
    for _ in range(max_steps):
        wait_out_queue(sb, cfg)
        if page_has_any_text(sb, S.BOOKING_SUCCESS_TEXTS):
            return
        if first_present(sb, S.REVIEW_CONFIRM_BTN, timeout=2):
            return
        # OTP in the middle of the flow?
        if first_visible(sb, S.OTP_INPUT, timeout=2):
            return
        cont = first_visible(sb, S.CONTINUE_BTN, timeout=3)
        if not cont:
            return
        sb.click(cont, by=by_of(cont))
        human_pause(1.5, 3)


def attempt_booking(sb, cfg, availability) -> BookingResult:
    """Drive the booking from a calendar page. `availability` is the result
    from monitor.check_availability() (used for preferred dates)."""
    preferred = list(getattr(availability, "dates", []) or [])
    screenshot(sb, cfg.screenshot_dir, "booking_start", cfg.screenshots_enabled)

    # 1. date + time
    chosen_date = _pick_date(sb, cfg, preferred)
    _pick_time_slot(sb, cfg)
    human_pause(1, 2)
    wait_out_queue(sb, cfg)

    # 2. walk to the applicant-details step
    _click_through_continues(sb, cfg)

    # 3. fill applicant details if that page is up
    if first_present(sb, list(next(iter(S.APPLICANT_FIELDS.values()))), timeout=3) or \
       any(first_present(sb, sels, timeout=1) for sels in S.APPLICANT_FIELDS.values()):
        _fill_applicant(sb, cfg)
        screenshot(sb, cfg.screenshot_dir, "applicant_filled", cfg.screenshots_enabled)
        # continue to review
        _click_through_continues(sb, cfg)

    # 4. mid-flow OTP?
    if first_visible(sb, S.OTP_INPUT, timeout=3):
        log.info("Confirmation OTP requested before finalising.")
        code = get_otp(cfg, prompt="Enter the BOOKING confirmation OTP VFS just sent")
        fill_otp_into_page(sb, code, S.OTP_INPUT, S.OTP_SUBMIT)
        human_pause(2, 4)
        wait_out_queue(sb, cfg)
        _click_through_continues(sb, cfg)

    # 5. final confirm / pay
    confirm = first_visible(sb, S.REVIEW_CONFIRM_BTN, timeout=8)
    if confirm:
        screenshot(sb, cfg.screenshot_dir, "before_confirm", cfg.screenshots_enabled)
        sb.click(confirm, by=by_of(confirm))
        log.info("Clicked the final Confirm button.")
        human_pause(3, 6)
        wait_out_queue(sb, cfg)
        # Some portals show one more OTP right at the end.
        if first_visible(sb, S.OTP_INPUT, timeout=4):
            log.info("Final OTP requested.")
            code = get_otp(cfg, prompt="Enter the FINAL confirmation OTP")
            fill_otp_into_page(sb, code, S.OTP_INPUT, S.OTP_SUBMIT)
            human_pause(3, 6)
    else:
        log.warning(
            "Reached the end of the flow but found no Confirm/Pay button. "
            "If running with --show, complete the last step manually now (you have ~60s)."
        )
        if not cfg.headless:
            for _ in range(12):
                time.sleep(5)
                if page_has_any_text(sb, S.BOOKING_SUCCESS_TEXTS):
                    break

    # 6. did it work?
    human_pause(2, 4)
    success_hit = page_has_any_text(sb, S.BOOKING_SUCCESS_TEXTS)
    ref = _extract_reference(sb)
    if success_hit or ref:
        path = screenshot(sb, cfg.screenshot_dir, "BOOKING_CONFIRMED", cfg.screenshots_enabled)
        log.info("BOOKING CONFIRMED! date=%s ref=%s screenshot=%s", chosen_date, ref or "(none found)", path)
        return BookingResult(booked=True, reference=ref, date=chosen_date,
                             note=success_hit or "confirmation page detected")
    screenshot(sb, cfg.screenshot_dir, "booking_outcome_unclear", cfg.screenshots_enabled)
    return BookingResult(
        booked=False,
        date=chosen_date,
        note="couldn't confirm the booking succeeded — check screenshots; the slot may "
             "have been taken, or a manual step (payment/extra OTP) is required.",
    )

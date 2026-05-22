"""Attempt to book a slot after monitor.check_availability() found one.

VFS flows vary. The current rus/ru/svn flow reports the nearest slot on the
application-detail page, then asks for applicant details before the calendar.
Other portals can still show the calendar first. This module supports both
orders before it reaches review / confirmation.

This is the most fragile part of any VFS bot because the applicant-details and
review pages differ a lot between portals. The bot fills the fields it
recognises (bot/selectors.py APPLICANT_FIELDS). After login the worker must not
wait for manual form completion; an unrecognised required step is a booking
failure that needs a selector/config update.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime

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
    dry_run: bool = False


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
def _normalise_applicant_value(sb, sel: str, key: str, value) -> str:
    text = str(value)
    if key == "phone_country_code":
        return text.lstrip("+")
    if key not in {"date_of_birth", "passport_expiry"}:
        return text
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    try:
        input_type = (sb.get_attribute(sel, "type", by=by_of(sel)) or "").lower()
    except Exception:
        input_type = ""
    if input_type == "date":
        return text
    return datetime.strptime(text, "%Y-%m-%d").strftime("%d%m%Y")


def _clear_and_type(sb, sel: str, text: str) -> None:
    try:
        from selenium.webdriver.common.keys import Keys

        els = sb.find_elements(sel, by=by_of(sel))
        visible = [el for el in els if el.is_displayed()]
        el = visible[0] if visible else (els[0] if els else None)
        if el:
            el.click()
            el.send_keys(Keys.CONTROL + "a")
            el.send_keys(Keys.BACKSPACE)
            try:
                el.clear()
            except Exception:
                pass
            el.send_keys(text)
            return
    except Exception as e:
        log.debug("Direct field typing failed for %s: %s", sel, e)
    sb.clear(sel, by=by_of(sel))
    sb.type(sel, text, by=by_of(sel))


def _option_aliases(value: str) -> list[str]:
    value = str(value).strip()
    low = value.lower()
    if low == "male":
        return [value, "\u041c\u0443\u0436\u0441\u043a\u043e\u0439"]
    if low == "female":
        return [value, "\u0416\u0435\u043d\u0441\u043a\u0438\u0439"]
    return [value]


def _pick_mat_option(sb, values: list[str]) -> bool:
    for value in values:
        xpaths = [
            f'//mat-option//span[normalize-space()="{value}"]',
            f'//mat-option[normalize-space()="{value}"]',
            f'//*[@role="option"][normalize-space()="{value}"]',
            f'//mat-option//span[contains(normalize-space(), "{value}")]',
            f'//*[@role="option"][contains(normalize-space(), "{value}")]',
        ]
        for xp in xpaths:
            if sb.is_element_present(xp, by="xpath"):
                sb.click(xp, by="xpath")
                return True
    return False


def _fill_applicant(sb, cfg, index: int = 0) -> None:
    """Fill the applicant-details form from config.applicants[index]."""
    applicants = cfg.applicants
    if not applicants:
        raise BookingError("VFS is asking for applicant details, but config.applicants is empty.")
    try:
        a = applicants[index]
    except IndexError as e:
        raise BookingError(
            f"VFS needs applicant #{index + 1}, but config.applicants has only {len(applicants)} item(s)."
        ) from e
    filled = []
    for key, sels in S.APPLICANT_FIELDS.items():
        val = a.get(key)
        if not val:
            continue
        sel = first_visible(sb, sels, timeout=2)
        if not sel:
            continue
        try:
            _clear_and_type(sb, sel, _normalise_applicant_value(sb, sel, key, val))
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
                if not _pick_mat_option(sb, _option_aliases(str(val))):
                    log.debug("Applicant select option not found for %r", val)
                    continue
            filled.append("gender/nationality")
            human_pause(0.2, 0.6)
        except Exception as e:
            log.debug("Couldn't set select %r: %s", val, e)
    if filled:
        log.info("Filled applicant #%d fields: %s", index + 1, ", ".join(sorted(set(filled))))
    else:
        log.warning("Couldn't fill any fields for applicant #%d; markup likely changed.", index + 1)


def _applicant_form_visible(sb) -> bool:
    if first_present(sb, S.YOUR_DETAILS_PAGE, timeout=1):
        return True
    return any(first_present(sb, sels, timeout=0.5) for sels in S.APPLICANT_FIELDS.values())


def _wait_your_details_save_delay(sb) -> None:
    """Honor the VFS warning that blocks very fast applicant saves."""
    try:
        src = sb.get_page_source()
    except Exception:
        return
    for pat in (
        r"\u043f\u043e\u0434\u043e\u0436\u0434\u0438\u0442\u0435\s+(\d+)\s+\u0441\u0435\u043a",
        r"wait\s+(\d+)\s+seconds",
    ):
        m = re.search(pat, src, flags=re.IGNORECASE)
        if m:
            seconds = min(int(m.group(1)) + 1, 60)
            if seconds > 0:
                log.info("Waiting %ds before saving applicant details.", seconds)
                time.sleep(seconds)
            return


def _save_your_details(sb, cfg) -> None:
    save = first_visible(sb, S.YOUR_DETAILS_SAVE_BTN, timeout=5)
    if not save:
        raise BookingError("Applicant details form has no visible Save button.")
    _wait_your_details_save_delay(sb)
    sb.click(save, by=by_of(save))
    log.info("Saved applicant details.")
    human_pause(2, 4)
    wait_out_queue(sb, cfg)
    from .monitor import _handle_appointment_captcha

    _handle_appointment_captcha(
        sb,
        cfg,
        website_url="https://lift-api.vfsglobal.com/appointment/applicants",
    )


def _target_applicant_count(cfg) -> int:
    return max(1, int(getattr(cfg, "applicants_count", 1)))


def _open_next_applicant_form(sb, cfg, index: int) -> None:
    add = first_visible(sb, S.ADD_APPLICANT_BTN, timeout=8)
    if not add:
        raise BookingError(
            f"Expected an Add Applicant button before applicant #{index + 1}, but none was visible."
        )
    sb.click(add, by=by_of(add))
    log.info("Opened applicant #%d form.", index + 1)
    human_pause(2, 4)
    wait_out_queue(sb, cfg)
    if not _applicant_form_visible(sb):
        raise BookingError(f"Clicked Add Applicant, but applicant #{index + 1} form did not appear.")


def _fill_applicants_if_visible(sb, cfg) -> int:
    if not _applicant_form_visible(sb):
        return 0

    count = _target_applicant_count(cfg)
    if len(cfg.applicants) < count:
        if count == 1 and not cfg.applicants:
            log.info("No applicant data configured; relying on portal pre-filled applicant form.")
            screenshot(sb, cfg.screenshot_dir, "applicant_1_prefilled", cfg.screenshots_enabled)
            _save_your_details(sb, cfg)
            return 1
        raise BookingError(
            f"appointment.applicants_count={count}, but config.applicants has only {len(cfg.applicants)} item(s)."
        )

    for index in range(count):
        if not _applicant_form_visible(sb):
            raise BookingError(f"Expected applicant #{index + 1} form, but it is not visible.")
        _fill_applicant(sb, cfg, index)
        screenshot(sb, cfg.screenshot_dir, f"applicant_{index + 1}_filled", cfg.screenshots_enabled)
        _save_your_details(sb, cfg)
        if index < count - 1:
            _open_next_applicant_form(sb, cfg, index + 1)
    return count


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
        try:
            if sb.get_attribute(cont, "disabled", by=by_of(cont)) not in (None, "", "false"):
                return
        except Exception:
            pass
        sb.click(cont, by=by_of(cont))
        human_pause(1.5, 3)


def _continue_from_application_detail(sb, cfg) -> None:
    """Enter the next VFS step after an application-detail slot banner."""
    if first_present(sb, S.CALENDAR_ROOT, timeout=1) or _applicant_form_visible(sb):
        return
    cont = first_visible(sb, S.CONTINUE_BTN, timeout=3)
    if not cont:
        return
    try:
        if sb.get_attribute(cont, "disabled", by=by_of(cont)) not in (None, "", "false"):
            return
    except Exception:
        pass
    sb.click(cont, by=by_of(cont))
    log.info("Continued from application details.")
    human_pause(2, 4)
    wait_out_queue(sb, cfg)
    # Some portal variants put the schedule Turnstile between these steps.
    from .monitor import _handle_appointment_captcha

    _handle_appointment_captcha(sb, cfg)


def _pick_calendar_step(sb, cfg, preferred: list[str]) -> str:
    chosen_date = _pick_date(sb, cfg, preferred)
    _pick_time_slot(sb, cfg)
    human_pause(1, 2)
    wait_out_queue(sb, cfg)
    return chosen_date


def _booking_otp(sb, cfg, prompt: str) -> None:
    if str(cfg.otp_mode).lower() == "manual":
        raise BookingError(
            "Booking OTP requested after login, but otp.mode=manual. "
            "Configure automatic OTP retrieval before auto-booking."
        )
    code = get_otp(cfg, prompt=prompt)
    fill_otp_into_page(sb, code, S.OTP_INPUT, S.OTP_SUBMIT)


def attempt_booking(sb, cfg, availability) -> BookingResult:
    """Drive booking after monitor.check_availability() found availability."""
    preferred = list(getattr(availability, "dates", []) or [])
    chosen_date = preferred[0] if preferred else ""
    calendar_picked = False
    screenshot(sb, cfg.screenshot_dir, "booking_start", cfg.screenshots_enabled)

    # Current Slovenia flow: available slot banner -> Continue -> your-details.
    # Calendar-first portals skip this and use the existing date picker path.
    if first_present(sb, S.CALENDAR_ROOT, timeout=2):
        chosen_date = _pick_calendar_step(sb, cfg, preferred)
        calendar_picked = True
        _click_through_continues(sb, cfg)
    else:
        _continue_from_application_detail(sb, cfg)

    _fill_applicants_if_visible(sb, cfg)

    # Applicant-first flows reach the appointment calendar after Save.
    if first_present(sb, S.CALENDAR_ROOT, timeout=5):
        chosen_date = _pick_calendar_step(sb, cfg, preferred)
        calendar_picked = True

    # Walk through intermediate steps after calendar or applicant details.
    _click_through_continues(sb, cfg)
    if not calendar_picked and first_present(sb, S.CALENDAR_ROOT, timeout=3):
        chosen_date = _pick_calendar_step(sb, cfg, preferred)
        calendar_picked = True
        _click_through_continues(sb, cfg)

    # Calendar-first variants can still place applicant details after the slot.
    if _fill_applicants_if_visible(sb, cfg):
        _click_through_continues(sb, cfg)

    # Mid-flow OTP?
    if first_visible(sb, S.OTP_INPUT, timeout=3):
        log.info("Confirmation OTP requested before finalising.")
        _booking_otp(sb, cfg, prompt="Read the BOOKING confirmation OTP VFS just sent")
        human_pause(2, 4)
        wait_out_queue(sb, cfg)
        _click_through_continues(sb, cfg)

    # Final confirm / pay.
    confirm = first_visible(sb, S.REVIEW_CONFIRM_BTN, timeout=8)
    if confirm:
        screenshot(sb, cfg.screenshot_dir, "before_confirm", cfg.screenshots_enabled)
        if getattr(cfg, "auto_book_dry_run", False):
            screenshot(sb, cfg.screenshot_dir, "dry_run_before_confirm", cfg.screenshots_enabled)
            log.info("Dry-run: stopped before final Confirm button.")
            return BookingResult(
                booked=False,
                dry_run=True,
                date=chosen_date,
                note="dry-run stopped before final Confirm button",
            )
        sb.click(confirm, by=by_of(confirm))
        log.info("Clicked the final Confirm button.")
        human_pause(3, 6)
        wait_out_queue(sb, cfg)
        # Some portals show one more OTP right at the end.
        if first_visible(sb, S.OTP_INPUT, timeout=4):
            log.info("Final OTP requested.")
            _booking_otp(sb, cfg, prompt="Read the FINAL confirmation OTP")
            human_pause(3, 6)
    else:
        log.warning(
            "Reached the end of the automated flow but found no Confirm/Pay button."
        )

    # Did it work?
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

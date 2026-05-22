"""Navigate from the dashboard to the appointment calendar and read availability.

Returns an AvailabilityResult: either "no slots", or one or more free dates
(and, if we could read them, the time slots on the first free date).

Also exposes inspect_options() used by `--inspect` to dump the exact dropdown
strings you need to put in config.yaml.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

from . import selectors as S
from .captcha import install_turnstile_hook, solve_cloudflare_clearance
from .login import (
    _solve_with_paid_service,
    _try_uc_click,
    _turnstile_present,
    is_queue_page,
    wait_out_queue,
)
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
class AvailabilityResult:
    available: bool = False
    dates: list[str] = field(default_factory=list)         # e.g. ["2026-06-12", ...]
    note: str = ""                                          # human-readable status
    # If we navigated all the way to a calendar, keep the sb on that page so
    # booking.py can continue without re-navigating.
    on_calendar: bool = False


class MonitorError(RuntimeError):
    pass


# --- mat-select helpers ----------------------------------------------------
def _open_select(sb, trigger_selectors, label: str):
    sel = first_visible(sb, trigger_selectors, timeout=8)
    if not sel:
        raise MonitorError(
            f"Couldn't find the '{label}' dropdown. Run `--inspect` and update "
            f"bot/selectors.py (SELECT_*_TRIGGER)."
        )
    sb.click(sel, by=by_of(sel))
    human_pause(0.5, 1.2)
    # wait for the overlay panel
    if not first_present(sb, S.MAT_OPTION_PANEL, timeout=4):
        # some builds render options inline; not fatal
        log.debug("No overlay panel detected after opening '%s' select.", label)
    return sel


def _pick_option(sb, value: str, label: str) -> None:
    """Click the mat-option whose visible text equals (or contains) `value`."""
    value = (value or "").strip()
    if not value:
        raise MonitorError(f"config.yaml: appointment.{label} is empty.")
    # exact match first, then contains
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
            human_pause(0.4, 1.0)
            log.info("Selected %s: %s", label, value)
            return
    # couldn't find it — dump what's available to help the user
    opts = _read_open_options(sb)
    raise MonitorError(
        f"Option '{value}' not found in the '{label}' dropdown. Available options:\n"
        + "\n".join(f"  - {o}" for o in opts)
        + f"\nFix appointment.{label} in config.yaml to match one of these exactly."
    )


def _read_open_options(sb) -> list[str]:
    """Read the texts of options in the currently-open mat-select panel."""
    out: list[str] = []
    for sel in ("mat-option", '[role="option"]'):
        try:
            els = sb.find_elements(sel, by="css selector")
        except Exception:
            els = []
        for el in els:
            try:
                t = (el.text or "").strip()
            except Exception:
                t = ""
            if t and t not in out:
                out.append(t)
        if out:
            break
    return out


def _close_overlay(sb) -> None:
    try:
        sb.press_keys("body", "")  # ESC
    except Exception:
        try:
            sb.click("body")
        except Exception:
            pass
    human_pause(0.2, 0.5)


# --- navigation ------------------------------------------------------------
def _go_to_booking_page(sb, cfg) -> None:
    """From the dashboard, click into the new-booking / schedule flow."""
    install_turnstile_hook(sb)
    wait_out_queue(sb, cfg)
    btn = first_visible(sb, S.START_BOOKING_BTN, timeout=10)
    if btn:
        sb.click(btn, by=by_of(btn))
        human_pause(2, 4)
        install_turnstile_hook(sb)
        wait_out_queue(sb, cfg)
    else:
        log.debug("No explicit 'start booking' button — assuming we're already on the form.")


def _select_appointment_params(sb, cfg) -> None:
    install_turnstile_hook(sb)
    _handle_appointment_captcha(sb, cfg)

    appt = cfg.appointment
    # The three dropdowns. Some portals don't have a sub-category; skip if absent.
    _open_select(sb, S.SELECT_CENTRE_TRIGGER, "visa_centre")
    _pick_option(sb, appt.get("visa_centre", ""), "visa_centre")

    if first_present(sb, S.SELECT_CATEGORY_TRIGGER, timeout=3):
        _open_select(sb, S.SELECT_CATEGORY_TRIGGER, "visa_category")
        _pick_option(sb, appt.get("visa_category", ""), "visa_category")

    if first_present(sb, S.SELECT_SUBCATEGORY_TRIGGER, timeout=3):
        sub = appt.get("visa_sub_category", "")
        if sub:
            _open_select(sb, S.SELECT_SUBCATEGORY_TRIGGER, "visa_sub_category")
            _pick_option(sb, sub, "visa_sub_category")

    # Current VFS portals may show either "no slots" or the nearest slot as
    # soon as the sub-category is selected. Decide that before clicking on.
    _handle_appointment_captcha(sb, cfg)


def _continue_after_params(sb, cfg) -> bool:
    """Click a real enabled Continue button when the portal needs it."""
    install_turnstile_hook(sb)
    cont = first_visible(sb, S.CONTINUE_BTN, timeout=3)
    if not cont:
        return False
    try:
        disabled = sb.get_attribute(cont, "disabled", by=by_of(cont))
        aria_disabled = sb.get_attribute(cont, "aria-disabled", by=by_of(cont))
    except Exception:
        disabled = aria_disabled = None
    if disabled not in (None, "", "false") or aria_disabled == "true":
        log.debug("Visible Continue control is disabled after parameter selection.")
        return False

    sb.click(cont, by=by_of(cont))
    human_pause(2, 4)
    wait_out_queue(sb, cfg)
    _handle_appointment_captcha(sb, cfg)
    return True


def _handle_appointment_captcha(
    sb,
    cfg,
    website_url: str = "https://lift-api.vfsglobal.com/appointment/CheckIsSlotAvailable",
) -> None:
    """Handle the captcha modal that VFS can show after appointment params."""
    install_turnstile_hook(sb)
    hit = _visible_appointment_captcha(sb)
    if not hit:
        return

    log.warning("Appointment captcha shown after selecting parameters; solving automatically.")
    screenshot(sb, cfg.screenshot_dir, "appointment_captcha", cfg.screenshots_enabled)
    _clear_appointment_turnstile(sb, cfg, website_url=website_url)

    submit = first_visible(sb, S.CAPTCHA_SUBMIT_BTN, timeout=5)
    if submit:
        sb.click(submit, by=by_of(submit))
        log.info("Submitted appointment captcha.")
        human_pause(2, 4)
        wait_out_queue(sb, cfg)

    if _visible_appointment_captcha(sb):
        screenshot(sb, cfg.screenshot_dir, "appointment_captcha_still_present", cfg.screenshots_enabled)
        raise MonitorError("Appointment captcha is still present after automatic solving.")


def _visible_appointment_captcha(sb) -> str | None:
    for text in S.APPOINTMENT_CAPTCHA_TEXTS:
        try:
            if sb.is_text_visible(text):
                return text
        except Exception:
            continue
    for sel in (
        "app-cloudflare-dialog",
        "app-cloudflare-captcha-container",
        "mat-dialog-container app-cloudflare-dialog",
    ):
        try:
            for el in sb.find_elements(sel, by="css selector"):
                if el.is_displayed():
                    text = (el.text or "").strip()
                    if text:
                        return text
        except Exception:
            continue
    return None


def _clear_appointment_turnstile(
    sb,
    cfg,
    website_url: str = "https://lift-api.vfsglobal.com/appointment/CheckIsSlotAvailable",
) -> None:
    """Clear Turnstile on the current appointment page without navigating away."""
    clearance_ok = cfg.captcha_enabled and solve_cloudflare_clearance(sb, cfg, website_url)
    if clearance_ok:
        log.info("Appointment Cloudflare API clearance injected.")

    if not _turnstile_present(sb, timeout=2):
        return

    _try_uc_click(sb)
    human_pause(1.5, 3.0)
    if not _turnstile_present(sb, timeout=2):
        log.info("Appointment captcha cleared by UC click.")
        return

    if cfg.captcha_enabled and _solve_with_paid_service(sb, cfg):
        log.info("Appointment captcha token injected by paid solver.")
        return
    log.warning("Appointment captcha remains after automatic attempts.")


# --- availability parsing --------------------------------------------------
_MONTHS = {
    m.lower(): i
    for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June",
         "July", "August", "September", "October", "November", "December"],
        start=1,
    )
}


def _calendar_label_month_year(sb) -> tuple[int, int] | None:
    """Read the 'June 2026' header of the mat-calendar, if present."""
    for sel in ('button.mat-calendar-period-button', '.mat-calendar-period-button', '.calendar-header'):
        try:
            if sb.is_element_present(sel, by="css selector"):
                txt = sb.get_text(sel, by="css selector")
                m = re.search(r"([A-Za-z]+)\s+(\d{4})", txt or "")
                if m and m.group(1).lower() in _MONTHS:
                    return _MONTHS[m.group(1).lower()], int(m.group(2))
        except Exception:
            continue
    return None


def _collect_calendar_dates(sb, cfg, max_months: int = 3) -> list[str]:
    """Walk up to `max_months` calendar pages, collecting enabled day cells."""
    found: list[str] = []
    if not first_present(sb, S.CALENDAR_ROOT, timeout=5):
        return found
    for _ in range(max_months):
        my = _calendar_label_month_year(sb)
        # enabled day buttons
        for sel in S.CALENDAR_DAY_AVAILABLE:
            try:
                els = sb.find_elements(sel, by=by_of(sel))
            except Exception:
                els = []
            for el in els:
                try:
                    day_txt = (el.text or el.get_attribute("aria-label") or "").strip()
                except Exception:
                    day_txt = ""
                day_num = re.search(r"\b(\d{1,2})\b", day_txt)
                if not day_num:
                    continue
                if my:
                    found.append(f"{my[1]:04d}-{my[0]:02d}-{int(day_num.group(1)):02d}")
                else:
                    found.append(day_txt)  # best-effort label
            if els:
                break
        # next month
        nxt = first_visible(sb, S.CALENDAR_NEXT_MONTH, timeout=2)
        if not nxt:
            break
        try:
            sb.click(nxt, by=by_of(nxt))
        except Exception:
            break
        human_pause(0.8, 1.6)
    # de-dupe, keep order
    seen = set()
    uniq = []
    for d in found:
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    return uniq


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _date_set(value) -> set[str]:
    if not value:
        return set()
    if isinstance(value, str):
        return {part.strip() for part in value.split(",") if part.strip()}
    return {str(part).strip() for part in value if str(part).strip()}


def _is_iso_date(value: str) -> bool:
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value or ""))


def _filter_by_window(dates: list[str], cfg) -> list[str]:
    appt = cfg.appointment
    any_date = _truthy(appt.get("any_date", False))
    lo = "" if any_date else (appt.get("earliest_date") or "").strip()
    hi = "" if any_date else (appt.get("latest_date") or "").strip()
    excluded = _date_set(appt.get("excluded_dates"))
    out = []
    for d in dates:
        if d in excluded:
            continue
        if not _is_iso_date(d):
            out.append(d)  # can't compare; don't drop it
            continue
        if lo and d < lo:
            continue
        if hi and d > hi:
            continue
        out.append(d)

    preference = str(appt.get("date_preference") or "").strip().lower()
    if preference == "random":
        random.shuffle(out)
        return out
    if preference not in {"earliest", "latest"}:
        return out

    iso = [d for d in out if _is_iso_date(d)]
    other = [d for d in out if not _is_iso_date(d)]
    iso.sort(reverse=preference == "latest")
    return iso + other


def _application_detail_slot_dates(sb) -> list[str] | None:
    """Read the nearest slot banner shown on the current application-detail page.

    Returns None when no nearest-slot banner is present. An empty list means
    that the portal said a slot exists but the displayed date could not be
    parsed from its current markup.
    """
    if not page_has_any_text(sb, S.NEAREST_SLOT_TEXTS):
        return None
    try:
        src = sb.get_page_source()
    except Exception:
        return []
    dates: list[str] = []
    for day, month, year in re.findall(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", src):
        date = f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        if date not in dates:
            dates.append(date)
    return dates


# --- public API ------------------------------------------------------------
def check_availability(sb, cfg) -> AvailabilityResult:
    """Full check: navigate to the calendar and report what's free.

    Caller must already be logged in (perform_login done on this `sb`).
    """
    install_turnstile_hook(sb)
    _go_to_booking_page(sb, cfg)

    # If just navigating dumped us in a queue and we couldn't get out:
    if is_queue_page(sb) and not wait_out_queue(sb, cfg):
        return AvailabilityResult(available=False, note="stuck in waiting room")

    try:
        _select_appointment_params(sb, cfg)
    except MonitorError as e:
        screenshot(sb, cfg.screenshot_dir, "param_select_failed", cfg.screenshots_enabled)
        raise

    human_pause(1.5, 3)
    wait_out_queue(sb, cfg)

    # Current VFS application-detail flows resolve availability immediately
    # after the sub-category is selected, before a calendar is reachable.
    no_slots_hit = page_has_any_text(sb, S.NO_SLOTS_TEXT)
    if no_slots_hit:
        return AvailabilityResult(available=False, note=f"portal says: {no_slots_hit}")

    application_dates = _application_detail_slot_dates(sb)
    if application_dates is not None:
        dates = _filter_by_window(application_dates, cfg)
        if application_dates and not dates:
            return AvailabilityResult(
                available=False,
                note="nearest application-detail slot is outside configured date window",
            )
        screenshot(sb, cfg.screenshot_dir, "slots_found", cfg.screenshots_enabled)
        return AvailabilityResult(
            available=True,
            dates=dates,
            note="portal shows a nearest available slot before applicant details",
            on_calendar=False,
        )

    # Other portal variants need Continue before a calendar / captcha appears.
    _continue_after_params(sb, cfg)
    human_pause(1.0, 2.0)
    wait_out_queue(sb, cfg)

    no_slots_hit = page_has_any_text(sb, S.NO_SLOTS_TEXT)
    if no_slots_hit:
        return AvailabilityResult(available=False, note=f"portal says: {no_slots_hit}")

    # Otherwise try to read the calendar.
    dates = _collect_calendar_dates(sb, cfg)
    dates = _filter_by_window(dates, cfg)
    on_cal = bool(first_present(sb, S.CALENDAR_ROOT, timeout=2))

    if dates:
        screenshot(sb, cfg.screenshot_dir, "slots_found", cfg.screenshots_enabled)
        return AvailabilityResult(
            available=True,
            dates=dates,
            note=f"{len(dates)} date(s) available",
            on_calendar=on_cal,
        )

    # No "no slots" text and no enabled days -> treat as no availability, but
    # screenshot it because it might mean the markup changed.
    screenshot(sb, cfg.screenshot_dir, "no_slots_or_unknown", cfg.screenshots_enabled)
    return AvailabilityResult(
        available=False,
        note="no enabled calendar dates (and no explicit message)",
        on_calendar=on_cal,
    )


def inspect_options(sb, cfg) -> None:
    """Print the exact strings available in each dropdown, for config.yaml."""
    _go_to_booking_page(sb, cfg)
    wait_out_queue(sb, cfg)
    print("\n" + "=" * 70)
    print(" DROPDOWN OPTIONS — copy these verbatim into config.yaml")
    print("=" * 70)

    triples = [
        ("visa_centre", S.SELECT_CENTRE_TRIGGER),
        ("visa_category", S.SELECT_CATEGORY_TRIGGER),
        ("visa_sub_category", S.SELECT_SUBCATEGORY_TRIGGER),
    ]
    # Selecting centre often populates category, which populates sub-category,
    # so do them in order, picking the configured value as we go (if set).
    appt = cfg.appointment
    for key, trig in triples:
        if not first_present(sb, trig, timeout=4):
            print(f"\n[{key}] (dropdown not present on this portal — skip)")
            continue
        try:
            _open_select(sb, trig, key)
        except MonitorError as e:
            print(f"\n[{key}] could not open: {e}")
            continue
        opts = _read_open_options(sb)
        print(f"\n[{key}] {len(opts)} option(s):")
        for o in opts:
            print(f"    {o!r}")
        _close_overlay(sb)
        # if the user already configured this one, select it so the next
        # dropdown gets populated correctly
        want = (appt.get(key) or "").strip()
        if want and want in opts:
            try:
                _open_select(sb, trig, key)
                _pick_option(sb, want, key)
            except Exception:
                pass
        human_pause(0.5, 1.2)
    print("\n" + "=" * 70 + "\n")
    screenshot(sb, cfg.screenshot_dir, "inspect", cfg.screenshots_enabled)

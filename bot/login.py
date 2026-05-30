"""Log in to the VFS Global portal: open page -> pass Turnstile -> credentials
-> login OTP -> land on the dashboard.

Raises LoginError on anything that needs operator attention (edge block, wrong
password, persistent rate-limit). The main loop decides whether to retry.
"""
from __future__ import annotations

import json
import random
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
from .constants import VFS_TURNSTILE_SITEKEY
from .otp import OTPError, fill_otp_into_page, get_otp
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
        marker = str(hit or "").lower()
        if "429201" in marker or "account blocked" in marker or "аккаунт заблокирован" in marker:
            raise RateLimited(
                f"VFS temporarily blocked this account/user ('{hit}') after too many requests. "
                "Wait until the cooldown expires or use another account."
            )
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
    "429201",
    "account blocked",
    "аккаунт заблокирован",
    "\u0434\u043e\u0441\u0442\u0443\u043f \u043e\u0433\u0440\u0430\u043d\u0438\u0447\u0435\u043d \u0434\u043b\u044f \u0438\u0434\u0435\u043d\u0442\u0438\u0444\u0438\u043a\u0430\u0442\u043e\u0440\u0430 \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044f",
    "\u043d\u0435\u043e\u0431\u044b\u0447\u043d\u0443\u044e \u0430\u043a\u0442\u0438\u0432\u043d\u043e\u0441\u0442\u044c",
    "\u0432\u0440\u0435\u043c\u0435\u043d\u043d\u043e \u043e\u0433\u0440\u0430\u043d\u0438\u0447\u0438\u043b\u0438 \u0434\u043e\u0441\u0442\u0443\u043f",
)
ACCOUNT_NOT_REGISTERED_TEXT_MARKERS = (
    "not registered",
    "not yet registered",
    "email address is not registered",
    "e-mail address is not registered",
    "адрес электронной почты не зарегистрирован",
    "почта не зарегистрирована",
    "не зарегистрирован у нас",
)
MOBILE_UPDATE_TEXT_MARKERS = (
    "mobile phone number",
    "update mobile",
    "update mobile number",
    "номер мобильного телефона",
    "обновить",
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


def _account_not_registered(sb) -> str:
    haystack = _page_haystack(sb)
    for marker in ACCOUNT_NOT_REGISTERED_TEXT_MARKERS:
        if marker in haystack:
            return marker
    return ""


def _mobile_update_prompt(sb) -> bool:
    haystack = _page_haystack(sb)
    if not any(marker in haystack for marker in MOBILE_UPDATE_TEXT_MARKERS):
        return False
    try:
        return bool(sb.execute_script(
            r"""
            return !!document.querySelector(
              'input[formcontrolname="contact"], input[formcontrolname="contactNo"], '
              + 'input[formcontrolname="dailcode"], input[formcontrolname="dialCode"], '
              + 'mat-select[formcontrolname="dailcode"], mat-select[formcontrolname="dialCode"]'
            );
            """
        ))
    except Exception:
        return True


def _mobile_update_success_page(sb) -> bool:
    haystack = _page_haystack(sb)
    return any(
        marker in haystack
        for marker in (
            "mobile number successfully updated",
            "mobile phone number successfully updated",
            "номер мобильного телефона успешно обновлен",
            "номер мобильного телефона успешно обновлён",
            "click here to login",
            "кликните здесь чтоб войти",
            "кликните здесь чтобы войти",
        )
    )


def _click_mobile_update_login_button(sb, timeout: float = 12.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            clicked = sb.execute_script(
                """
                const visible = el => !!el && el.offsetWidth > 0 && el.offsetHeight > 0;
                const terms = ['click here to login', 'login', 'sign in', 'войти', 'кликните здесь'];
                const items = Array.from(document.querySelectorAll('button, a, [role="button"]')).filter(visible);
                const item = items.find(el => {
                  const text = (el.innerText || el.textContent || '').trim().toLowerCase();
                  return terms.some(term => text.includes(term));
                });
                if (!item) return false;
                item.scrollIntoView({block: 'center', inline: 'center'});
                for (const name of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                  item.dispatchEvent(new MouseEvent(name, {bubbles: true, cancelable: true, view: window}));
                }
                return true;
                """
            )
            if clicked:
                return True
        except Exception as e:
            log.debug("Could not click mobile update login button: %s", e)
        human_pause(0.4, 0.8)
    return False


def _captcha_modal_visible(sb) -> bool:
    try:
        return bool(sb.execute_script(
            """
            const visible = el => !!el && el.offsetWidth > 0 && el.offsetHeight > 0;
            return Array.from(document.querySelectorAll(
              'mat-dialog-container,.mat-mdc-dialog-container,[role="dialog"],.modal,.cdk-overlay-pane'
            )).some(el => {
              if (!visible(el)) return false;
              const text = (el.innerText || el.textContent || '').toLowerCase();
              return /captcha|капч|подтверд/.test(text);
            });
            """
        ))
    except Exception:
        return False


def _confirm_login_captcha_modal(sb, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _captcha_modal_visible(sb):
            return False
        try:
            clicked = sb.execute_script(
                """
                const visible = el => !!el && el.offsetWidth > 0 && el.offsetHeight > 0;
                const dialogs = Array.from(document.querySelectorAll(
                  'mat-dialog-container,.mat-mdc-dialog-container,[role="dialog"],.modal,.cdk-overlay-pane'
                )).filter(visible);
                const scopes = dialogs.length ? dialogs : [document];
                for (const scope of scopes) {
                  const text = (scope.innerText || scope.textContent || '').toLowerCase();
                  if (!/captcha|капч|подтверд/.test(text)) continue;
                  const btn = Array.from(scope.querySelectorAll('button')).find(el => {
                    if (!visible(el)) return false;
                    const label = (el.innerText || el.textContent || '').trim().toLowerCase();
                    return ['submit', 'confirm', 'continue', 'подтвердить', 'продолжить'].some(t => label.includes(t));
                  });
                  if (!btn) continue;
                  btn.disabled = false;
                  btn.removeAttribute('disabled');
                  btn.scrollIntoView({block: 'center', inline: 'center'});
                  for (const name of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                    btn.dispatchEvent(new MouseEvent(name, {bubbles: true, cancelable: true, view: window}));
                  }
                  return true;
                }
                return false;
                """
            )
        except Exception as e:
            log.debug("Could not click login captcha modal: %s", e)
            clicked = False
        if clicked:
            human_pause(1, 2)
            if not _captcha_modal_visible(sb):
                log.info("Confirmed VFS captcha modal.")
                return True
        time.sleep(0.5)
    if _captcha_modal_visible(sb):
        raise LoginError("VFS captcha confirmation modal did not close after clicking Submit.")
    return False


def _random_mobile_update_number(cfg, dial_code: str = "") -> str:
    dial = str(dial_code or "").strip().lstrip("+")
    if dial == "44":
        # VFS often defaults to +44 on this route. Use a UK mobile-looking
        # national number so client-side validators accept the selected code.
        return "7" + "".join(str(random.randint(0, 9)) for _ in range(9))
    if dial == "1":
        return str(random.randint(2, 9)) + "".join(str(random.randint(0, 9)) for _ in range(9))

    phone_cfg = (getattr(cfg, "registration_cfg", {}) or {}).get("random_phone") or {}
    digits = max(7, int(phone_cfg.get("digits") or 10))
    prefixes = phone_cfg.get("prefixes") or ["900", "901", "902", "903", "904", "905", "906", "909"]
    prefix = str(random.choice(prefixes))
    suffix_len = max(0, digits - len(prefix))
    suffix = "".join(str(random.randint(0, 9)) for _ in range(suffix_len))
    return (prefix + suffix)[:digits]


def _mobile_update_dial_code(cfg) -> str:
    reg = getattr(cfg, "registration_cfg", {}) or {}
    phone_cfg = reg.get("random_phone") or {}
    raw = (
        reg.get("phone_country_code")
        or (reg.get("defaults") or {}).get("phone_country_code")
        or phone_cfg.get("country_code")
        or "+7"
    )
    return str(raw).strip().lstrip("+") or "7"


def _read_mobile_update_dial_code(sb) -> str:
    try:
        code = sb.execute_script(
            r"""
            const visible = el => !!el && el.offsetWidth > 0 && el.offsetHeight > 0;
            const input = Array.from(document.querySelectorAll(
              'input[formcontrolname="dialCode"], input[formcontrolname="dailcode"], input[formcontrolname="phoneCountryCode"]'
            )).find(visible);
            if (input && String(input.value || '').trim()) return String(input.value).trim();

            const hosts = Array.from(document.querySelectorAll(
              'mat-select[formcontrolname="dialCode"], mat-select[formcontrolname="dailcode"], '
              + '[formcontrolname="dialCode"], [formcontrolname="dailcode"], [role="combobox"]'
            )).filter(visible);
            for (const host of hosts) {
              const text = (host.innerText || host.textContent || '').trim();
              const match = text.match(/\+?\d{1,4}/);
              if (match) return match[0];
            }

            const valueText = Array.from(document.querySelectorAll(
              '.mat-mdc-select-value-text, .mat-select-value-text, .mat-mdc-select-min-line'
            )).find(visible);
            if (valueText) {
              const match = (valueText.innerText || valueText.textContent || '').match(/\+?\d{1,4}/);
              if (match) return match[0];
            }
            return '';
            """
        )
    except Exception:
        return ""
    return str(code or "").strip().lstrip("+")


def _select_mobile_update_dial_code(sb, dial_code: str) -> bool:
    code = str(dial_code or "").strip().lstrip("+")
    if not code:
        return False
    try:
        selected = sb.execute_script(
            r"""
            const code = String(arguments[0] || '').replace(/^\+/, '');
            const visible = el => !!el && el.offsetWidth > 0 && el.offsetHeight > 0;
            const matches = text => {
              const value = String(text || '').replace(/\s+/g, ' ').trim();
              return new RegExp('(^|\\D)\\+?' + code + '(\\D|$)').test(value);
            };

            const select = Array.from(document.querySelectorAll(
              'select[formcontrolname="dialCode"], select[formcontrolname="dailcode"], select[formcontrolname="phoneCountryCode"]'
            )).find(visible);
            if (select) {
              const option = Array.from(select.options).find(opt => matches(opt.textContent) || matches(opt.value));
              if (!option) return false;
              select.value = option.value;
              select.dispatchEvent(new Event('input', {bubbles: true}));
              select.dispatchEvent(new Event('change', {bubbles: true}));
              select.dispatchEvent(new Event('blur', {bubbles: true}));
              return true;
            }

            const hosts = Array.from(document.querySelectorAll(
              'mat-select[formcontrolname="dialCode"], mat-select[formcontrolname="dailcode"], '
              + '[formcontrolname="dialCode"][role="combobox"], [formcontrolname="dailcode"][role="combobox"], '
              + 'mat-select, [role="combobox"]'
            )).filter(visible);
            const host = hosts.find(el => matches(el.innerText || el.textContent)) || hosts[0];
            if (!host) return false;
            host.scrollIntoView({block: 'center', inline: 'center'});
            for (const name of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
              host.dispatchEvent(new MouseEvent(name, {bubbles: true, cancelable: true, view: window}));
            }
            return true;
            """,
            code,
        )
    except Exception as e:
        log.debug("Could not open mobile dial-code dropdown: %s", e)
        selected = False
    if not selected:
        return False

    human_pause(0.5, 1.0)
    try:
        return bool(sb.execute_script(
            r"""
            const code = String(arguments[0] || '').replace(/^\+/, '');
            const visible = el => !!el && el.offsetWidth > 0 && el.offsetHeight > 0;
            const matches = text => {
              const value = String(text || '').replace(/\s+/g, ' ').trim();
              return new RegExp('(^|\\D)\\+?' + code + '(\\D|$)').test(value);
            };
            const options = Array.from(document.querySelectorAll(
              'mat-option, .mat-mdc-option, [role="option"], .cdk-overlay-pane li, .cdk-overlay-pane button'
            )).filter(visible);
            const option = options.find(el => matches(el.innerText || el.textContent));
            if (!option) {
              document.body.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
              return false;
            }
            option.scrollIntoView({block: 'center', inline: 'center'});
            for (const name of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
              option.dispatchEvent(new MouseEvent(name, {bubbles: true, cancelable: true, view: window}));
            }
            return true;
            """,
            code,
        ))
    except Exception as e:
        log.debug("Could not select mobile dial-code option: %s", e)
        return False


_MOBILE_UPDATE_SYNC_JS = r"""return (() => {
const dialCode = String(arguments[0] || '7').replace(/^\+/, '');
const phone = String(arguments[1] || '');
const token = String(arguments[2] || window.__vfsTurnstileToken || '');
const state = {
  dialSet: false,
  phoneSet: false,
  captchaSet: false,
  angularSynced: 0,
  formInvalid: null,
  dialValid: null,
  phoneValid: null,
  submitDisabled: null,
};

function visible(el) {
  return !!el && el.offsetWidth > 0 && el.offsetHeight > 0;
}

function first(selectors) {
  for (const sel of selectors) {
    const items = Array.from(document.querySelectorAll(sel));
    const el = items.find(visible) || items[0];
    if (el) return el;
  }
  return null;
}

function setNativeValue(el, value) {
  if (!el) return false;
  try {
    const proto = el.tagName === 'TEXTAREA'
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    if (setter) setter.call(el, value);
    else el.value = value;
    for (const name of ['focus', 'input', 'change', 'keyup', 'blur']) {
      el.dispatchEvent(new Event(name, { bubbles: true }));
    }
    return true;
  } catch (e) {
    return false;
  }
}

function setControl(control, value) {
  if (!control) return false;
  try {
    if (typeof control.setValue === 'function') control.setValue(value, { emitEvent: true });
    else if (typeof control.patchValue === 'function') control.patchValue(value, { emitEvent: true });
    else control.value = value;
  } catch (e) {
    try { control.setValue(value); } catch (_e) { try { control.value = value; } catch (__e) {} }
  }
  try { if (typeof control.markAsDirty === 'function') control.markAsDirty(); } catch (e) {}
  try { if (typeof control.markAsTouched === 'function') control.markAsTouched(); } catch (e) {}
  try { if (typeof control.updateValueAndValidity === 'function') control.updateValueAndValidity({ emitEvent: true }); } catch (e) {}
  return true;
}

function syncForm(form) {
  if (!form || !form.controls) return false;
  const controls = form.controls;
  let synced = false;
  for (const name of ['dialCode', 'dailcode', 'phoneCountryCode']) {
    if (controls[name]) synced = setControl(controls[name], dialCode) || synced;
  }
  for (const name of ['contactNo', 'contact', 'contactNumber', 'phoneNumber']) {
    if (controls[name]) synced = setControl(controls[name], phone) || synced;
  }
  if (token && controls.captcha_api_key) {
    synced = setControl(controls.captcha_api_key, token) || synced;
    state.captchaSet = true;
  }
  if (token && controls.captcha_version) {
    synced = setControl(controls.captcha_version, 'cloudflare') || synced;
    state.captchaSet = true;
  }
  if (synced) {
    state.angularSynced += 1;
    try { state.formInvalid = !!form.invalid; } catch (e) {}
    try { state.dialValid = controls.dialCode ? !!controls.dialCode.valid : controls.dailcode ? !!controls.dailcode.valid : null; } catch (e) {}
    try { state.phoneValid = controls.contactNo ? !!controls.contactNo.valid : controls.contact ? !!controls.contact.valid : null; } catch (e) {}
    try { if (typeof form.markAllAsTouched === 'function') form.markAllAsTouched(); } catch (e) {}
    try { if (typeof form.updateValueAndValidity === 'function') form.updateValueAndValidity({ emitEvent: true }); } catch (e) {}
  }
  return synced;
}

function syncCaptcha(obj) {
  if (!token || !obj) return;
  for (const name of ['submitV2Captcha', 'submitV2CaptchaUpdateMobileNumberOTP']) {
    try {
      if (typeof obj[name] === 'function') {
        obj[name](token);
        state.captchaSet = true;
      }
    } catch (e) {}
  }
  for (const name of ['v2CaptchaSet', 'recaptchaSuccess', 'captchaSuccess']) {
    try {
      const emitter = obj[name];
      if (emitter && typeof emitter.emit === 'function') {
        emitter.emit(token);
        state.captchaSet = true;
      }
    } catch (e) {}
  }
}

function scan(obj, depth, seen) {
  if (!obj || depth > 3 || typeof obj !== 'object' || seen.has(obj)) return;
  seen.add(obj);
  if (typeof Node !== 'undefined' && obj instanceof Node) return;
  syncCaptcha(obj);
  try { if (obj.loginForm) syncForm(obj.loginForm); } catch (e) {}
  try { if (obj.updatesForm) syncForm(obj.updatesForm); } catch (e) {}
  try { syncForm(obj); } catch (e) {}
  try {
    const control = obj.control || obj._control || obj.formControl || null;
    const name = String(obj.name || obj._name || obj.formControlName || obj.ngControlName || '').toLowerCase();
    if (control && ['dialcode', 'dailcode', 'phonecountrycode'].includes(name)) {
      if (setControl(control, dialCode)) state.angularSynced += 1;
    }
    if (control && ['contactno', 'contact', 'contactnumber', 'phonenumber'].includes(name)) {
      if (setControl(control, phone)) state.angularSynced += 1;
    }
    if (token && control && name === 'captcha_api_key') {
      if (setControl(control, token)) {
        state.angularSynced += 1;
        state.captchaSet = true;
      }
    }
    if (token && control && name === 'captcha_version') {
      if (setControl(control, 'cloudflare')) {
        state.angularSynced += 1;
        state.captchaSet = true;
      }
    }
  } catch (e) {}
  let keys = [];
  try { keys = Object.keys(obj).slice(0, 60); } catch (e) { return; }
  for (const key of keys) {
    let value;
    try { value = obj[key]; } catch (e) { continue; }
    if (!value || typeof value !== 'object') continue;
    if (value.controls) syncForm(value);
    else if (depth < 2) scan(value, depth + 1, seen);
  }
}

const dialEl = first([
  'input[formcontrolname="dialCode"]',
  'input[formcontrolname="dailcode"]',
  'input[formcontrolname="phoneCountryCode"]',
  'input[name="dialCode"]',
  'input[name="dailcode"]'
]);
const phoneEl = first([
  'input[formcontrolname="contactNo"]',
  'input[formcontrolname="contact"]',
  'input[formcontrolname="contactNumber"]',
  'input[formcontrolname="phoneNumber"]',
  'input[name="contactNo"]',
  'input[name="phone"]',
  'input[type="tel"]'
]);
state.dialSet = !dialEl || setNativeValue(dialEl, dialCode);
state.phoneSet = setNativeValue(phoneEl, phone);

const roots = [];
for (const el of document.querySelectorAll('*')) {
  try {
    if (window.ng && typeof window.ng.getComponent === 'function') {
      const component = window.ng.getComponent(el);
      if (component) roots.push(component);
    }
  } catch (e) {}
  try {
    if (window.ng && typeof window.ng.getDirectives === 'function') {
      const directives = window.ng.getDirectives(el) || [];
      directives.forEach(item => { if (item && typeof item === 'object') roots.push(item); });
    }
  } catch (e) {}
  try {
    const ctx = el.__ngContext__;
    if (ctx && typeof ctx.length === 'number') {
      for (let i = 0; i < ctx.length; i++) {
        const item = ctx[i];
        if (item && typeof item === 'object') roots.push(item);
      }
    }
  } catch (e) {}
}
const seen = new WeakSet();
for (const root of roots.slice(0, 300)) scan(root, 0, seen);
if (token) {
  try { (window.__vfsTurnstileParams || []).forEach(item => item && item.callback && item.callback(token)); } catch (e) {}
  document.querySelectorAll(
    'input[name="cf-turnstile-response"], #cf-turnstile-response, input[name="g-recaptcha-response"], textarea[name="g-recaptcha-response"]'
  ).forEach(el => setNativeValue(el, token));
}

const btn = first([
  'button[type="submit"]',
  'button.btn-brand-orange',
  'button.mat-focus-indicator.btn-block',
  'button.mdc-button'
]);
if (btn) state.submitDisabled = !!(btn.disabled || btn.hasAttribute('disabled') || btn.getAttribute('aria-disabled') === 'true');
return state;
})();"""


def _sync_mobile_update_form(sb, dial_code: str, phone_number: str, token: str = "") -> dict:
    try:
        state = sb.execute_script(_MOBILE_UPDATE_SYNC_JS, dial_code, phone_number, token or "")
    except Exception as e:
        log.debug("Could not sync mobile update form: %s", e)
        return {}
    return state if isinstance(state, dict) else {}


def _type_mobile_update_phone(sb, phone_number: str) -> bool:
    selectors = [
        'input[formcontrolname="contactNo"]',
        'input[formcontrolname="contact"]',
        'input[formcontrolname="contactNumber"]',
        'input[formcontrolname="phoneNumber"]',
        'input[name="contactNo"]',
        'input[name="phone"]',
        'input[type="tel"]',
    ]
    sel = first_visible(sb, selectors, timeout=5)
    if not sel:
        return False
    by = by_of(sel)
    try:
        sb.click(sel, by=by)
        human_pause(0.2, 0.5)
        sb.clear(sel, by=by)
    except Exception:
        try:
            sb.execute_script(
                """
                const el = document.querySelector(arguments[0]);
                if (!el) return;
                el.focus();
                el.select && el.select();
                el.value = '';
                el.dispatchEvent(new Event('input', {bubbles: true}));
                """,
                sel,
            )
        except Exception:
            pass
    for ch in phone_number:
        try:
            sb.send_keys(sel, ch, by=by)
        except Exception:
            return False
        human_pause(0.03, 0.09)
    try:
        sb.execute_script(
            """
            const el = document.querySelector(arguments[0]);
            if (!el) return;
            el.dispatchEvent(new Event('change', {bubbles: true}));
            el.dispatchEvent(new Event('blur', {bubbles: true}));
            """,
            sel,
        )
    except Exception:
        pass
    return True


def _click_mobile_update_submit(sb, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    selectors = [
        '//button[contains(normalize-space(), "Обновить")]',
        '//button[contains(normalize-space(), "Update")]',
        '//button[contains(normalize-space(), "Continue")]',
        '//button[contains(normalize-space(), "Submit")]',
        'button[type="submit"]',
        'button.btn-brand-orange',
    ]
    while time.time() < deadline:
        for sel in selectors:
            btn = first_visible(sb, [sel], timeout=0.5)
            if not btn:
                continue
            try:
                sb.scroll_to(btn, by=by_of(btn))
            except Exception:
                pass
            try:
                sb.click(btn, by=by_of(btn))
                return True
            except Exception as e:
                log.debug("Could not click mobile update submit %s via Selenium: %s", sel, e)
        try:
            clicked = sb.execute_script(
                """
                const visible = el => !!el && el.offsetWidth > 0 && el.offsetHeight > 0;
                const positive = ['continue', 'submit', 'update', 'save', 'verify', 'next',
                  'продолж', 'отправ', 'обнов', 'сохран', 'подтверд', 'далее'];
                const buttons = Array.from(document.querySelectorAll('button'))
                  .filter(el => visible(el));
                let btn = buttons.find(el => {
                  const text = (el.innerText || el.textContent || '').trim().toLowerCase();
                  return positive.some(term => text.includes(term));
                }) || buttons.find(el => el.type === 'submit') || null;
                if (!btn) return false;
                btn.disabled = false;
                btn.removeAttribute('disabled');
                btn.removeAttribute('aria-disabled');
                btn.classList.remove('mat-mdc-button-disabled');
                btn.classList.remove('btn-brand-ash');
                btn.scrollIntoView({block: 'center', inline: 'center'});
                for (const name of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                  btn.dispatchEvent(new MouseEvent(name, {bubbles: true, cancelable: true, view: window}));
                }
                try {
                  if (btn.form && typeof btn.form.requestSubmit === 'function') btn.form.requestSubmit(btn);
                  else if (btn.form) btn.form.dispatchEvent(new Event('submit', {bubbles: true, cancelable: true}));
                } catch (e) {}
                return true;
                """
            )
            if clicked:
                return True
        except Exception as e:
            log.debug("Could not click mobile update submit via JS: %s", e)
        human_pause(0.4, 0.8)
    return False


def _handle_mobile_update_prompt(sb, cfg) -> None:
    page_dial_code = _read_mobile_update_dial_code(sb)
    dial_code = page_dial_code or _mobile_update_dial_code(cfg)
    if _select_mobile_update_dial_code(sb, dial_code):
        selected_code = _read_mobile_update_dial_code(sb)
        if selected_code:
            dial_code = selected_code
        log.info("Selected mobile dial code +%s.", dial_code)
    else:
        log.warning("Could not explicitly select mobile dial code +%s; continuing with visible/default value.", dial_code)
    phone_number = _random_mobile_update_number(cfg, dial_code)
    token = _current_turnstile_token(sb)
    if token:
        _apply_login_turnstile_token(sb, token)
    log.info("Entering random mobile number for VFS profile update (dial code +%s).", dial_code)
    if not _type_mobile_update_phone(sb, phone_number):
        screenshot(sb, cfg.screenshot_dir, "mobile_update_phone_missing", cfg.screenshots_enabled)
        raise LoginError("VFS requested a mobile number update, but the phone field was not found.")
    human_pause(0.4, 0.8)
    state = _sync_mobile_update_form(sb, dial_code, phone_number, token)
    if not state.get("phoneSet"):
        screenshot(sb, cfg.screenshot_dir, "mobile_update_phone_missing", cfg.screenshots_enabled)
        raise LoginError("VFS requested a mobile number update, but the phone field was not found.")
    human_pause(0.5, 1.2)
    state = _sync_mobile_update_form(sb, dial_code, phone_number, token)
    log.debug("Mobile update sync state before submit: %s", state)
    if not _click_mobile_update_submit(sb):
        screenshot(sb, cfg.screenshot_dir, "mobile_update_submit_missing", cfg.screenshots_enabled)
        raise LoginError("VFS requested a mobile number update, but the submit button was not found.")
    deadline = time.time() + 60
    handled_otp = False
    while time.time() < deadline:
        human_pause(1.0, 2.0)
        _check_edge_block(sb, cfg)
        wait_out_queue(sb, cfg)
        _check_access_restricted(sb, cfg)
        if not _mobile_update_prompt(sb):
            log.info("VFS mobile-number update prompt closed.")
            return
        otp_sel = first_visible(sb, S.OTP_INPUT, timeout=1)
        if otp_sel and not handled_otp:
            log.info("Mobile-number update OTP requested.")
            try:
                code = get_otp(cfg, prompt="Enter the VFS mobile update OTP")
            except OTPError as e:
                raise LoginError(str(e)) from e
            if not fill_otp_into_page(sb, code, S.OTP_INPUT, S.OTP_SUBMIT):
                raise LoginError("Could not enter the mobile update OTP.")
            handled_otp = True
            continue
        err = first_visible(sb, S.LOGIN_ERROR, timeout=1)
        if err:
            try:
                txt = sb.get_text(err, by=by_of(err)).strip()
            except Exception:
                txt = ""
            if txt:
                screenshot(sb, cfg.screenshot_dir, "mobile_update_error", cfg.screenshots_enabled)
                raise LoginError(f"VFS rejected the random mobile number: {txt}")

    screenshot(sb, cfg.screenshot_dir, "mobile_update_still_visible", cfg.screenshots_enabled)
    raise LoginError("VFS mobile-number update prompt stayed visible after submitting a random number.")


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


def _turnstile_present(sb, timeout: float = 3.0) -> bool:
    """Detect Turnstile even when Cloudflare renders it in a closed shadow root."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if first_present(sb, S.TURNSTILE_IFRAME, timeout=0.25):
            return True
        try:
            active = sb.execute_script(
                """
                return !!(
                  document.querySelector('[data-sitekey], .cf-turnstile, input[name="cf-turnstile-response"]') ||
                  Array.from(document.querySelectorAll('iframe')).some(f => {
                    const src = f.getAttribute('src') || '';
                    const title = f.getAttribute('title') || '';
                    return src.includes('challenges.cloudflare.com') || title.includes('Cloudflare');
                  })
                );
                """
            )
            if active:
                return True
        except Exception:
            pass
        try:
            src = (sb.get_page_source() or "").lower()
        except Exception:
            src = ""
        try:
            form_visible = bool(sb.execute_script(
                """
                return !!document.querySelector(
                  'input[formcontrolname="username"], input[type="email"], input[name="username"]'
                );
                """
            ))
        except Exception:
            form_visible = False
        if form_visible:
            return False
        if "challenges.cloudflare.com" in src and (
            "cf-chl-widget" in src or "cf-turnstile-response" in src or "cf_chl" in src
        ):
            return True
        time.sleep(0.25)
    return False


def _wait_for_turnstile_auto_clear(sb, timeout: float = 6.0) -> bool:
    """Wait briefly for Cloudflare to clear Turnstile without GUI interaction."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _turnstile_present(sb, timeout=0.5):
            return True
        time.sleep(0.5)
    return not _turnstile_present(sb, timeout=0.5)


def _cdp_runtime_value(sb, expression: str):
    driver = getattr(sb, "driver", None)
    if not driver or not hasattr(driver, "execute_cdp_cmd"):
        return None
    try:
        result = driver.execute_cdp_cmd(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
    except Exception as e:
        log.debug("CDP Runtime.evaluate failed: %s", e)
        return None
    return result.get("result", {}).get("result", {}).get("value")


def _stop_page_loading(sb) -> None:
    driver = getattr(sb, "driver", None)
    if driver and hasattr(driver, "execute_cdp_cmd"):
        try:
            driver.execute_cdp_cmd("Page.stopLoading", {})
            return
        except Exception as e:
            log.debug("CDP Page.stopLoading failed: %s", e)
    try:
        sb.execute_script("window.stop();")
    except Exception:
        pass


def _navigate_without_wait(sb, url: str) -> bool:
    driver = getattr(sb, "driver", None)
    if driver and hasattr(driver, "execute_cdp_cmd"):
        try:
            driver.execute_cdp_cmd("Page.navigate", {"url": url})
            return True
        except Exception as e:
            log.debug("CDP Page.navigate failed: %s", e)
    try:
        sb.open(url)
        return True
    except Exception as e:
        log.debug("sb.open failed: %s", e)
        return False


def _angular_shell_empty(sb) -> bool:
    state = _cdp_runtime_value(
        sb,
        """
        (() => {
          const bodyText = (document.body && document.body.innerText || '').trim();
          const appRoot = document.querySelector('app-root');
          return {
            readyState: document.readyState,
            bodyTextLen: bodyText.length,
            appRoot: !!appRoot,
            appTextLen: appRoot ? (appRoot.innerText || '').trim().length : 0,
            hasLogin: !!document.querySelector(
              'input[formcontrolname="username"], input[type="email"], input[name="username"]'
            ),
            hasTurnstile: !!document.querySelector(
              'iframe[src*="challenges.cloudflare.com"], input[name="cf-turnstile-response"]'
            )
          };
        })()
        """,
    )
    if not isinstance(state, dict):
        return False
    return (
        bool(state.get("appRoot"))
        and not state.get("hasLogin")
        and not state.get("hasTurnstile")
        and int(state.get("appTextLen") or 0) == 0
    )


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
    if len(src) < 200 and compact in {
        "<html><head></head><body></body></html>",
        "<html><head></head><body></body></html>",
    }:
        return True

    if "vfsglobal.com" in url and _angular_shell_empty(sb):
        log.warning("VFS returned an empty Angular shell; page assets did not finish loading.")
        return True

    return False


def _login_form_or_turnstile_present(sb) -> bool:
    """True once the login page is actually usable (form rendered or a Turnstile
    challenge is showing). Used to tell a real load apart from a page that is
    still loading through a stalled proxy."""
    if first_visible(sb, S.LOGIN_EMAIL, timeout=0.5):
        return True
    return _turnstile_present(sb, timeout=0.5)


def _wait_for_login_page_usable(sb, timeout: float = 25.0) -> bool:
    """After a fire-and-forget navigation, wait up to `timeout` for the page to
    either become usable (form/Turnstile) or reveal a Chrome error.

    A stalled SOCKS5 proxy only paints ``ERR_TIMED_OUT`` after Chrome's own
    connection timeout (~30-60s), long after the short post-navigation pause.
    Returns True if the page is usable, False if it is blank/error or never
    rendered anything within the timeout (caller should then retry).
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _page_looks_blank_or_error(sb):
            return False
        if _login_form_or_turnstile_present(sb):
            return True
        time.sleep(0.5)
    # Timed out with neither a usable page nor a recognised error — treat as
    # not-usable so the caller falls back to the retrying open path.
    return _login_form_or_turnstile_present(sb)


def _open_login_page(sb, cfg, reconnect_seconds: int = 5) -> None:
    """Open the login page with retry logic for unstable proxies.

    Tries UC reconnect first, then falls back to normal open, with up to
    3 total attempts to handle proxy connection resets.
    """
    max_retries = 3
    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        # Try UC reconnect first (best for Cloudflare bypass)
        try:
            sb.uc_open_with_reconnect(cfg.login_url, reconnect_seconds)
        except Exception as e:
            last_error = e
            log.debug("uc_open_with_reconnect failed (attempt %d): %s", attempt, e)
            if not _navigate_without_wait(sb, cfg.login_url):
                log.debug("fallback navigation also failed (attempt %d).", attempt)
                if attempt < max_retries:
                    _stop_page_loading(sb)
                    human_pause(2.0, 4.0)
                    continue
                raise LoginError(
                    f"Could not open VFS login page after {max_retries} attempts: {e}"
                ) from e

        human_pause(1.5, 3.0)

        if _page_looks_blank_or_error(sb):
            log.warning(
                "Page blank/error after load (attempt %d/%d); retrying…",
                attempt, max_retries
            )
            # Try a simple open as fallback
            _stop_page_loading(sb)
            if not _navigate_without_wait(sb, cfg.login_url):
                last_error = RuntimeError("fallback navigation failed")
                log.debug("fallback navigation failed")
            human_pause(2.0, 4.0)

            if _page_looks_blank_or_error(sb):
                if attempt < max_retries:
                    _stop_page_loading(sb)
                    human_pause(3.0, 6.0)
                    continue
                raise LoginError(
                    f"VFS login page stayed blank/error after {max_retries} attempts."
                ) from last_error
            else:
                log.info("Page loaded successfully via fallback navigation.")
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
            chl_page_data=meta.get("chlPageData") or None,
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
    """Solve Cloudflare Turnstile without moving the real OS mouse.

    Order of attempts:
      1) Wait briefly for Cloudflare to auto-clear the challenge.
      2) Use the configured paid solver, currently CapSolver, and inject the token.

    SeleniumBase GUI captcha helpers are intentionally not used because they
    move the real Windows mouse and slow down the bot.
    """
    install_turnstile_hook(sb)
    try:
        existing_token = sb.execute_script("return window.__vfsTurnstileToken || '';")
    except Exception:
        existing_token = ""
    if not isinstance(existing_token, str):
        existing_token = ""
    if existing_token:
        inject_turnstile_token(sb, existing_token)
        human_pause(1.0, 2.0)
        return
    if not _turnstile_present(sb, timeout=3):
        return

    log.info("Cloudflare Turnstile present; waiting for automatic clearance without GUI click.")
    if _wait_for_turnstile_auto_clear(sb, timeout=6):
        log.info("Turnstile cleared automatically.")
        return

    if not cfg.captcha_enabled:
        screenshot(sb, cfg.screenshot_dir, "turnstile_solver_not_configured", cfg.screenshots_enabled)
        raise LoginError(
            "Turnstile did not clear automatically, and no captcha solver is configured. "
            "GUI captcha clicking is disabled; configure captcha.provider='capsolver' and api_key."
        )

    log.info("Turnstile did not clear automatically; using captcha service: %s", cfg.captcha_provider)
    if not _solve_with_paid_service(sb, cfg):
        screenshot(sb, cfg.screenshot_dir, "turnstile_solver_failed", cfg.screenshots_enabled)
        raise LoginError("Captcha solver could not solve or inject the Turnstile token.")

    # The iframe can remain in the DOM after a valid token is injected, so form
    # validation later decides whether the page accepted it.
    if _wait_for_turnstile_auto_clear(sb, timeout=10):
        log.info("Turnstile cleared by captcha solver.")
    else:
        log.warning("Captcha token injected, but Turnstile widget is still present; continuing to form validation.")


def _prepare_login_turnstile_stub(sb, cfg) -> str:
    if not cfg.captcha_enabled:
        return ""
    try:
        solver = get_solver(cfg)
    except CaptchaError as e:
        log.warning("Captcha solver disabled: %s", e)
        return ""
    if solver is None:
        return ""
    sitekey = str(cfg.raw.get("captcha", {}).get("turnstile_sitekey") or VFS_TURNSTILE_SITEKEY)
    attempts = max(1, int(cfg.raw.get("captcha", {}).get("turnstile_retries") or 3))
    last_error: CaptchaError | None = None
    for attempt in range(1, attempts + 1):
        try:
            log.info("Preparing login Turnstile token (attempt %d/%d).", attempt, attempts)
            token = solver.solve_turnstile(sitekey, cfg.login_url)
            _install_turnstile_preload_stub(sb, token)
            return token
        except CaptchaError as e:
            last_error = e
            log.warning("Login Turnstile preload solve failed: %s", e)
    if last_error:
        log.warning("Could not prepare login Turnstile stub: %s", last_error)
    return ""


def _install_turnstile_preload_stub(sb, token: str) -> None:
    source = _turnstile_stub_source(token)
    driver = getattr(sb, "driver", None)
    if driver and hasattr(driver, "execute_cdp_cmd"):
        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": source})
        except Exception as e:
            log.debug("Could not register login Turnstile stub preload: %s", e)
    try:
        sb.execute_script(source)
    except Exception as e:
        log.debug("Could not install login Turnstile stub on current page: %s", e)


def _turnstile_stub_source(token: str) -> str:
    token_json = json.dumps(token)
    return f"""
    (() => {{
      const token = {token_json};
      const stub = {{
        render(container, params = {{}}) {{
          try {{
            const el = typeof container === 'string' ? document.querySelector(container) : container;
            if (el && el.setAttribute) el.setAttribute('data-vfs-turnstile-stub', 'true');
          }} catch (e) {{}}
          setTimeout(() => {{
            try {{
              if (typeof params.callback === 'function') params.callback(token);
            }} catch (e) {{}}
          }}, 50);
          return 'vfs-login-stub-' + Date.now();
        }},
        getResponse() {{ return token; }},
        isExpired() {{ return false; }},
        reset() {{}},
        remove() {{}},
      }};
      window.__vfsTurnstileToken = token;
      window.__vfsTurnstileStub = stub;
      function applyToken() {{
        try {{
          if (typeof window.recaptchaCallback === 'function') window.recaptchaCallback();
        }} catch (e) {{}}
        try {{
          (window.__vfsTurnstileParams || []).forEach(item => {{
            try {{
              if (item && typeof item.callback === 'function') item.callback(token);
            }} catch (e) {{}}
          }});
        }} catch (e) {{}}
        try {{
          document.querySelectorAll(
            'input[name="cf-turnstile-response"], #cf-turnstile-response, input[name="g-recaptcha-response"], textarea[name="g-recaptcha-response"]'
          ).forEach(el => {{
            el.value = token;
            el.dispatchEvent(new Event('input', {{ bubbles: true }}));
            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
            el.dispatchEvent(new Event('blur', {{ bubbles: true }}));
          }});
        }} catch (e) {{}}
      }}
      try {{
        Object.defineProperty(window, 'turnstile', {{
          configurable: true,
          get() {{ return stub; }},
          set(_value) {{}}
        }});
      }} catch (e) {{
        window.turnstile = stub;
      }}
      setTimeout(applyToken, 50);
      setTimeout(applyToken, 500);
      setTimeout(applyToken, 1500);
    }})();
    """


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


def _wait_for_submit_enabled(sb, submit_sel: str, timeout: int = 15) -> bool:
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
                return True  # button is enabled
        except Exception:
            return False  # element gone / selector stale
        time.sleep(0.5)
    log.warning(
        "Submit button still disabled after %ds; Turnstile may not have completed.",
        timeout,
    )
    return False


def _click_login_submit(sb, timeout: int = 15) -> None:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        submit_sel = first_visible(sb, S.LOGIN_SUBMIT, timeout=3)
        if not submit_sel:
            human_pause(0.3, 0.7)
            continue
        try:
            # Force-enable and click the Sign In button via JS in case Angular's model is lagging,
            # or the button is scrolled off-screen / covered by overlapping elements.
            clicked = sb.execute_script("""
                const selectors = [
                    "button[type='submit']",
                    "button.btn-brand-orange",
                    "button.mat-focus-indicator.btn-block",
                    "button.mdc-button"
                ];
                let btn = null;
                for (const sel of selectors) {
                    btn = document.querySelector(sel);
                    if (btn) break;
                }
                if (!btn) {
                    btn = Array.from(document.querySelectorAll('button')).find(el => {
                        const t = (el.innerText || el.textContent || '').trim().toLowerCase();
                        return ['sign in', 'login', 'войти', 'вход'].some(term => t.includes(term));
                    });
                }
                if (btn) {
                    btn.disabled = false;
                    btn.removeAttribute('disabled');
                    btn.classList.remove('mat-mdc-button-disabled');
                    btn.classList.remove('btn-brand-ash');
                    btn.click();
                    return true;
                }
                return false;
            """)
            if clicked:
                log.info("Clicked login submit button via JavaScript.")
                human_pause(3, 6)
                return
        except Exception as e:
            log.debug("Could not force-enable or click login submit button via JS: %s", e)
        try:
            sb.click(submit_sel, by=by_of(submit_sel))
            return
        except Exception as e:
            last_error = e
            log.debug("Login submit click failed, retrying: %s", e)
            human_pause(0.4, 0.9)
    if last_error:
        raise LoginError(f"Could not click the Sign In button: {last_error}") from last_error
    raise LoginError("Could not find the Sign In button - update selectors.")


def _clear_and_type_login(sb, sel: str, text: str) -> None:
    try:
        from selenium.webdriver.common.keys import Keys

        matches = sb.find_elements(sel, by=by_of(sel))
        visible = [el for el in matches if el.is_displayed()]
        el = visible[0] if visible else (matches[0] if matches else None)
        if el:
            el.click()
            el.send_keys(Keys.CONTROL + "a")
            el.send_keys(Keys.BACKSPACE)
            try:
                el.clear()
            except Exception:
                pass
            el.send_keys(text)
            _sync_login_input_value(sb, el, text)
            return
    except Exception as e:
        log.debug("Direct login typing failed for %s: %s", sel, e)
    sb.clear(sel, by=by_of(sel))
    sb.type(sel, text, by=by_of(sel))
    try:
        matches = sb.find_elements(sel, by=by_of(sel))
        visible = [el for el in matches if el.is_displayed()]
        el = visible[0] if visible else (matches[0] if matches else None)
        if el:
            _sync_login_input_value(sb, el, text)
    except Exception:
        pass


def _sync_login_input_value(sb, element, text: str) -> None:
    try:
        sb.execute_script(
            """
            const el = arguments[0];
            const value = arguments[1];
            if (!el) return;
            const proto = el.tagName === 'TEXTAREA'
              ? HTMLTextAreaElement.prototype
              : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
            if (setter) setter.call(el, value);
            else el.value = value;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            el.dispatchEvent(new Event('blur', { bubbles: true }));
            """,
            element,
            str(text),
        )
    except Exception as e:
        log.debug("Could not sync login input value with Angular: %s", e)


_LOGIN_FORM_SYNC_JS = r"""return (() => {
const email = String(arguments[0] || '');
const password = String(arguments[1] || '');
const token = String(arguments[2] || window.__vfsTurnstileToken || '');
const state = {
  domSynced: 0,
  angularSynced: 0,
  directiveSynced: 0,
  captchaSynced: false,
  formFound: false,
  rootsScanned: 0,
  formInvalid: null,
  usernameValid: null,
  passwordValid: null,
  captchaApiKeyValid: null,
  captchaApiKeyLength: 0,
  captchaVersionLength: 0,
  submitDisabled: null,
  emailValueLength: null,
  passwordValueLength: null,
  captchaTokenLength: token.length,
  hiddenCaptchaLength: 0,
};

function first(selectors) {
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (el) return el;
  }
  return null;
}

function setNativeValue(el, value) {
  if (!el) return false;
  try {
    const proto = el.tagName === 'TEXTAREA'
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    if (setter) setter.call(el, value);
    else el.value = value;
    for (const name of ['focus', 'input', 'change', 'keyup', 'blur']) {
      el.dispatchEvent(new Event(name, { bubbles: true }));
    }
    return true;
  } catch (e) {
    return false;
  }
}

function setControl(control, value) {
  if (!control) return false;
  try {
    if (typeof control.setValue === 'function') control.setValue(value, { emitEvent: true });
    else if (typeof control.patchValue === 'function') control.patchValue(value, { emitEvent: true });
    else control.value = value;
  } catch (e) {
    try { control.setValue(value); } catch (_e) { try { control.value = value; } catch (__e) {} }
  }
  try { if (typeof control.markAsDirty === 'function') control.markAsDirty(); } catch (e) {}
  try { if (typeof control.markAsTouched === 'function') control.markAsTouched(); } catch (e) {}
  try { if (typeof control.updateValueAndValidity === 'function') control.updateValueAndValidity({ emitEvent: true }); } catch (e) {}
  return true;
}

function syncNamedControl(name, control) {
  if (!control || !name) return false;
  const key = String(name).toLowerCase().replace(/[-_\s]/g, '');
  if (['username', 'email', 'emailid'].includes(key)) return setControl(control, email);
  if (key === 'password') return setControl(control, password);
  if (token && key === 'captchaapikey') {
    state.captchaSynced = true;
    return setControl(control, token);
  }
  if (token && key === 'captchaversion') {
    state.captchaSynced = true;
    return setControl(control, 'cloudflare');
  }
  return false;
}

function captchaVersionFrom(owner, form) {
  const candidates = [];
  try { candidates.push(owner && owner.captchaVersionConst && owner.captchaVersionConst.cloudflare); } catch (e) {}
  try { candidates.push(owner && owner.captchaVersion); } catch (e) {}
  try { candidates.push(owner && owner.currentCaptcha); } catch (e) {}
  try { candidates.push(form && form.controls && form.controls.captcha_version && form.controls.captcha_version.value); } catch (e) {}
  candidates.push('cloudflare');
  for (const value of candidates) {
    if (value !== undefined && value !== null && String(value).trim()) return String(value);
  }
  return 'cloudflare';
}

function syncForm(form, owner = null) {
  if (!form || !form.controls) return false;
  const controls = form.controls;
  const username = controls.username || controls.email || controls.emailid;
  const pwd = controls.password;
  const captchaApiKey = controls.captcha_api_key;
  const captchaVersion = controls.captcha_version;
  let synced = false;
  if (username) synced = setControl(username, email) || synced;
  if (pwd) synced = setControl(pwd, password) || synced;
  if (token && captchaApiKey) {
    synced = setControl(captchaApiKey, token) || synced;
    state.captchaSynced = true;
  }
  if (token && captchaVersion) {
    synced = setControl(captchaVersion, captchaVersionFrom(owner, form)) || synced;
    state.captchaSynced = true;
  }
  try { if (typeof form.markAsDirty === 'function') form.markAsDirty(); } catch (e) {}
  try { if (typeof form.markAllAsTouched === 'function') form.markAllAsTouched(); } catch (e) {}
  try { if (typeof form.updateValueAndValidity === 'function') form.updateValueAndValidity({ emitEvent: true }); } catch (e) {}
  if (synced) {
    state.formFound = true;
    state.angularSynced += 1;
    try { state.formInvalid = !!form.invalid; } catch (e) {}
    try { state.usernameValid = username ? !!username.valid : null; } catch (e) {}
    try { state.passwordValid = pwd ? !!pwd.valid : null; } catch (e) {}
    try { state.captchaApiKeyValid = captchaApiKey ? !!captchaApiKey.valid : null; } catch (e) {}
    try { state.captchaApiKeyLength = captchaApiKey ? String(captchaApiKey.value || '').length : 0; } catch (e) {}
    try { state.captchaVersionLength = captchaVersion ? String(captchaVersion.value || '').length : 0; } catch (e) {}
  }
  return synced;
}

function syncDirective(obj, owner = null) {
  if (!obj || typeof obj !== 'object') return false;
  let synced = false;
  try {
    const form = obj.form || obj.formGroup || obj.ngForm || null;
    if (form && form.controls) synced = syncForm(form, owner || obj) || synced;
  } catch (e) {}
  try {
    const control = obj.control || obj._control || obj.formControl || null;
    const name = obj.name || obj._name || obj.formControlName || obj.ngControlName || null;
    if (control && syncNamedControl(name, control)) {
      synced = true;
      state.directiveSynced += 1;
      try {
        const parent = control.parent || control._parent || null;
        if (parent && parent.controls) syncForm(parent, owner || obj);
      } catch (e) {}
    }
  } catch (e) {}
  return synced;
}

function syncCaptcha(obj) {
  if (!token || !obj) return;
  try {
    if (typeof obj.submitV2Captcha === 'function') {
      obj.submitV2Captcha(token);
      state.captchaSynced = true;
    }
  } catch (e) {}
  for (const name of ['v2CaptchaSet', 'recaptchaSuccess', 'captchaSuccess']) {
    try {
      const emitter = obj[name];
      if (emitter && typeof emitter.emit === 'function') {
        emitter.emit(token);
        state.captchaSynced = true;
      }
    } catch (e) {}
  }
}

function scan(obj, depth, seen) {
  if (!obj || depth > 3 || typeof obj !== 'object' || seen.has(obj)) return;
  seen.add(obj);
  if (typeof Node !== 'undefined' && obj instanceof Node) return;
  state.rootsScanned += 1;
  syncDirective(obj, obj);
  syncCaptcha(obj);
  try { if (obj.loginForm) syncForm(obj.loginForm, obj); } catch (e) {}
  try { syncForm(obj, obj); } catch (e) {}
  let keys = [];
  try { keys = Object.keys(obj).slice(0, 60); } catch (e) { return; }
  for (const key of keys) {
    let value;
    try { value = obj[key]; } catch (e) { continue; }
    if (!value || typeof value !== 'object') continue;
    if (value.controls && (value.controls.username || value.controls.password || value.controls.captcha_api_key)) {
      syncForm(value, obj);
    } else if (depth < 2) {
      scan(value, depth + 1, seen);
    }
  }
}

const emailEl = first([
  'input[formcontrolname="username"]',
  'input#email',
  'input[type="email"]',
  'input[name="username"]'
]);
const passwordEl = first([
  'input[formcontrolname="password"]',
  'input#password',
  'input[type="password"]',
  'input[name="password"]'
]);
if (setNativeValue(emailEl, email)) state.domSynced += 1;
if (setNativeValue(passwordEl, password)) state.domSynced += 1;

if (token) {
  window.__vfsTurnstileToken = token;
  document.querySelectorAll(
    'input[name="cf-turnstile-response"], #cf-turnstile-response, input[name="g-recaptcha-response"], textarea[name="g-recaptcha-response"]'
  ).forEach(el => {
    setNativeValue(el, token);
    state.hiddenCaptchaLength = Math.max(state.hiddenCaptchaLength, String(el.value || '').length);
  });
  try { (window.__vfsTurnstileParams || []).forEach(item => item && item.callback && item.callback(token)); } catch (e) {}
}

const roots = [];
for (const el of document.querySelectorAll('*')) {
  try {
    if (window.ng && typeof window.ng.getComponent === 'function') {
      const component = window.ng.getComponent(el);
      if (component) roots.push(component);
    }
  } catch (e) {}
  try {
    if (window.ng && typeof window.ng.getDirectives === 'function') {
      const directives = window.ng.getDirectives(el) || [];
      directives.forEach(item => { if (item && typeof item === 'object') roots.push(item); });
    }
  } catch (e) {}
  try {
    const ctx = el.__ngContext__;
    if (ctx && typeof ctx.length === 'number') {
      for (let i = 0; i < ctx.length; i++) {
        const item = ctx[i];
        if (item && typeof item === 'object') roots.push(item);
      }
    }
  } catch (e) {}
}

for (const el of document.querySelectorAll('[formcontrolname]')) {
  const attrName = el.getAttribute('formcontrolname') || '';
  try {
    const ctx = el.__ngContext__;
    if (ctx && typeof ctx.length === 'number') {
      for (let i = 0; i < ctx.length; i++) {
        const item = ctx[i];
        if (!item || typeof item !== 'object') continue;
        roots.push(item);
        try {
          if (item.control) syncNamedControl(item.name || attrName, item.control);
        } catch (e) {}
      }
    }
  } catch (e) {}
}

const seen = new WeakSet();
for (const root of roots.slice(0, 300)) scan(root, 0, seen);

try { state.emailValueLength = emailEl ? String(emailEl.value || '').length : null; } catch (e) {}
try { state.passwordValueLength = passwordEl ? String(passwordEl.value || '').length : null; } catch (e) {}
try {
  document.querySelectorAll(
    'input[name="cf-turnstile-response"], #cf-turnstile-response, input[name="g-recaptcha-response"], textarea[name="g-recaptcha-response"]'
  ).forEach(el => {
    state.hiddenCaptchaLength = Math.max(state.hiddenCaptchaLength, String(el.value || '').length);
  });
} catch (e) {}
try {
  const btn = first([
    "button[type='submit']",
    'button.btn-brand-orange',
    'button.mat-focus-indicator.btn-block',
    'button.mdc-button'
  ]);
  if (btn) state.submitDisabled = !!(btn.disabled || btn.hasAttribute('disabled') || btn.getAttribute('aria-disabled') === 'true');
} catch (e) {}
return state;
})();"""


def _current_turnstile_token(sb) -> str:
    try:
        token = sb.execute_script(
            """
            return window.__vfsTurnstileToken
              || document.querySelector('input[name="cf-turnstile-response"]')?.value
              || document.querySelector('input[name="g-recaptcha-response"]')?.value
              || document.querySelector('textarea[name="g-recaptcha-response"]')?.value
              || '';
            """
        )
    except Exception:
        return ""
    return token if isinstance(token, str) else ""


def _apply_login_turnstile_token(sb, token: str) -> bool:
    if not token:
        return False
    applied = inject_turnstile_token(sb, token)
    try:
        applied = bool(sb.execute_script(
            """
            const token = arguments[0];
            window.__vfsTurnstileToken = token;
            try {
              if (typeof window.recaptchaCallback === 'function') window.recaptchaCallback();
            } catch (e) {}
            try {
              (window.__vfsTurnstileParams || []).forEach(item => {
                try { if (item && typeof item.callback === 'function') item.callback(token); } catch (e) {}
              });
            } catch (e) {}
            document.querySelectorAll(
              'input[name="cf-turnstile-response"], #cf-turnstile-response, input[name="g-recaptcha-response"], textarea[name="g-recaptcha-response"]'
            ).forEach(el => {
              const proto = el.tagName === 'TEXTAREA'
                ? HTMLTextAreaElement.prototype
                : HTMLInputElement.prototype;
              const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
              if (setter) setter.call(el, token);
              else el.value = token;
              el.dispatchEvent(new Event('input', { bubbles: true }));
              el.dispatchEvent(new Event('change', { bubbles: true }));
            });
            return true;
            """,
            token,
        )) or applied
    except Exception as e:
        log.debug("Could not apply login Turnstile token to Angular captcha: %s", e)
    return applied


def _sync_login_form(sb, email: str, password: str, token: str = "") -> dict:
    try:
        state = sb.execute_script(_LOGIN_FORM_SYNC_JS, email, password, token or "")
    except Exception as e:
        log.debug("Could not sync login form with Angular: %s", e)
        return {}
    return state if isinstance(state, dict) else {}


def _login_form_has_valid_credentials(state: dict) -> bool:
    if not state:
        return False
    email_len = int(state.get("emailValueLength") or 0)
    password_len = int(state.get("passwordValueLength") or 0)
    if email_len <= 0 or password_len <= 0:
        return False
    if state.get("usernameValid") is False or state.get("passwordValid") is False:
        return False
    if state.get("captchaApiKeyValid") is False:
        return False
    if state.get("formInvalid") is True:
        return False
    return True


def _wait_for_login_form_ready(
    sb,
    cfg,
    email: str,
    password: str,
    token: str = "",
    timeout: float = 18.0,
) -> dict:
    deadline = time.time() + timeout
    last_state: dict = {}
    while time.time() < deadline:
        token = token or _current_turnstile_token(sb)
        if token:
            _apply_login_turnstile_token(sb, token)
        last_state = _sync_login_form(sb, email, password, token)
        if _login_form_has_valid_credentials(last_state):
            if last_state.get("submitDisabled") is True:
                human_pause(0.4, 0.8)
                continue
            return last_state
        human_pause(0.4, 0.8)

    if _login_form_has_valid_credentials(last_state):
        log.warning(
            "Login form controls are valid in Angular, but the submit button stayed disabled; "
            "attempting forced click. state=%s",
            last_state,
        )
        return last_state

    screenshot(sb, cfg.screenshot_dir, "login_form_invalid", cfg.screenshots_enabled)
    raise LoginError(
        "Login form stayed invalid after credential sync: "
        f"formFound={last_state.get('formFound')} "
        f"formInvalid={last_state.get('formInvalid')} "
        f"usernameValid={last_state.get('usernameValid')} "
        f"passwordValid={last_state.get('passwordValid')} "
        f"captchaValid={last_state.get('captchaApiKeyValid')} "
        f"emailLen={last_state.get('emailValueLength')} "
        f"passwordLen={last_state.get('passwordValueLength')} "
        f"captchaControlLen={last_state.get('captchaApiKeyLength')} "
        f"captchaVersionLen={last_state.get('captchaVersionLength')} "
        f"captchaLen={last_state.get('hiddenCaptchaLength') or last_state.get('captchaTokenLength')}"
    )


def perform_login(sb, cfg, *, _after_mobile_update: bool = False) -> None:
    """Drive the full login. Assumes a fresh `sb` from open_browser()."""
    state_file = cfg.session_state_file
    if looks_logged_in(sb):
        log.info("Browser already appears logged in; skipping login form.")
        if state_file and cfg.session_export_enabled:
            save_browser_state(sb, cfg, state_file)
        return

    if state_file and cfg.session_import_enabled and not _after_mobile_update:
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
    login_turnstile_token = _prepare_login_turnstile_stub(sb, cfg)
    # uc_open_with_reconnect briefly disconnects the driver so CF sees a "real"
    # navigation — this is the SB-recommended way to load CF-protected pages.
    opened_with_preload = False
    if login_turnstile_token:
        log.info("Opening login page with preloaded Turnstile stub.")
        if _navigate_without_wait(sb, url):
            # Don't trust a fire-and-forget navigation after only a few seconds:
            # a stalled SOCKS5 proxy keeps the page "loading" and only paints
            # ERR_TIMED_OUT ~30-60s later. Wait until the page is actually
            # usable (form/Turnstile) or clearly errored before committing.
            if _wait_for_login_page_usable(sb, timeout=25.0):
                opened_with_preload = True
            else:
                log.warning(
                    "Preloaded login navigation did not yield a usable page "
                    "(blank/error or proxy stall); falling back to UC reconnect."
                )
                _stop_page_loading(sb)
    if not opened_with_preload:
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
    _clear_and_type_login(sb, email_sel, cfg.email)
    human_pause()
    _clear_and_type_login(sb, pwd_sel, cfg.password)
    human_pause()

    login_turnstile_token = login_turnstile_token or _current_turnstile_token(sb)
    if login_turnstile_token:
        _apply_login_turnstile_token(sb, login_turnstile_token)
    state = _wait_for_login_form_ready(
        sb,
        cfg,
        cfg.email,
        cfg.password,
        login_turnstile_token,
    )
    log.debug("Login form sync state before submit: %s", state)

    _click_login_submit(sb, timeout=45)
    log.info("Submitted login form.")
    human_pause(3, 6)
    _confirm_login_captcha_modal(sb)
    human_pause(1, 2)

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

    not_registered = _account_not_registered(sb)
    if not_registered:
        screenshot(sb, cfg.screenshot_dir, "login_error", cfg.screenshots_enabled)
        raise LoginError(f"VFS says account is not registered: {not_registered}")

    # --- login OTP ---------------------------------------------------------
    otp_sel = first_visible(sb, S.OTP_INPUT, timeout=8)
    if otp_sel:
        log.info("Login OTP requested.")
        # let the operator / IMAP fetch it
        try:
            code = get_otp(cfg, prompt="Enter the LOGIN OTP VFS just emailed you")
        except OTPError as e:
            raise LoginError(str(e)) from e
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
    if _mobile_update_prompt(sb):
        log.info("Login accepted; VFS is showing the mobile-number update prompt.")
        _handle_mobile_update_prompt(sb, cfg)
        human_pause(2, 4)
        _check_edge_block(sb, cfg)
        wait_out_queue(sb, cfg)
        _check_access_restricted(sb, cfg)
        if _mobile_update_success_page(sb):
            if _after_mobile_update:
                raise LoginError("VFS returned to the mobile-number success page after retrying login.")
            log.info("Mobile number updated; clicking VFS login button and logging in again.")
            if not _click_mobile_update_login_button(sb):
                screenshot(sb, cfg.screenshot_dir, "mobile_update_login_button_missing", cfg.screenshots_enabled)
                raise LoginError("Mobile number updated, but the follow-up login button was not found.")
            human_pause(2, 4)
            perform_login(sb, cfg, _after_mobile_update=True)
            return

    if _mobile_update_success_page(sb):
        if _after_mobile_update:
            raise LoginError("VFS stayed on the mobile-number success page after retrying login.")
        log.info("Mobile number already updated; clicking VFS login button and logging in again.")
        if not _click_mobile_update_login_button(sb):
            screenshot(sb, cfg.screenshot_dir, "mobile_update_login_button_missing", cfg.screenshots_enabled)
            raise LoginError("Mobile number updated, but the follow-up login button was not found.")
        human_pause(2, 4)
        perform_login(sb, cfg, _after_mobile_update=True)
        return

    if not looks_logged_in(sb):
        # give the SPA a moment to settle / navigate
        for _ in range(6):
            time.sleep(3)
            if looks_logged_in(sb):
                break
    if not looks_logged_in(sb):
        screenshot(sb, cfg.screenshot_dir, "post_login_unknown", cfg.screenshots_enabled)
        if _is_login_url(_page_url(sb)) or first_visible(sb, S.LOGIN_EMAIL, timeout=1):
            raise LoginError(
                "Login submit left the browser on the login page. "
                "Turnstile or credentials were not accepted."
            )
        log.warning(
            "Logged in but didn't find the expected dashboard control. "
            "Will still try to proceed — check screenshots if monitoring fails."
        )
    else:
        log.info("Login successful — on the dashboard.")

    if state_file and cfg.session_export_enabled:
        save_browser_state(sb, cfg, state_file)


def auto_login(sb, cfg) -> None:
    """Force credential-based login even when the config is set to manual login."""
    session_cfg = cfg.raw.setdefault("session", {})
    had_manual_login = "manual_login" in session_cfg
    previous_manual_login = session_cfg.get("manual_login")
    session_cfg["manual_login"] = False
    try:
        perform_login(sb, cfg)
    finally:
        if had_manual_login:
            session_cfg["manual_login"] = previous_manual_login
        else:
            session_cfg.pop("manual_login", None)


def wait_for_manual_login(sb, cfg, *, state_file=None, timeout_seconds: int | None = None) -> None:
    """Open VFS and wait until the operator finishes logging in."""
    timeout = int(timeout_seconds or cfg.manual_login_wait_seconds)
    log.info("Opening %s for manual login; waiting up to %ds.", cfg.login_url, timeout)
    login_turnstile_token = _prepare_login_turnstile_stub(sb, cfg)
    _open_login_page(sb, cfg)
    if login_turnstile_token:
        _install_turnstile_preload_stub(sb, login_turnstile_token)
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

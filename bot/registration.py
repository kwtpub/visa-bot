"""VFS account registration through the browser UI."""
from __future__ import annotations

import json
import random
import re
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import selectors as S
from .captcha import CaptchaError, extract_turnstile_sitekey, get_solver, solve_cloudflare_clearance
from .constants import VFS_TURNSTILE_SITEKEY
from .login import (
    LoginError,
    _check_edge_block,
    _dismiss_cookie_banner,
    _navigate_without_wait,
    _open_login_page,
    _pass_turnstile,
    _stop_page_loading,
    wait_out_queue,
)
from .otp import OTPError, fill_otp_into_page, get_email_link, get_otp
from .util import (
    by_of,
    first_visible,
    human_pause,
    log,
    page_has_any_text,
    screenshot,
    xpath_literal,
)


class RegistrationError(RuntimeError):
    pass


class RegistrationAlreadyExists(RegistrationError):
    pass

@dataclass
class RegistrationProfile:
    email: str
    password: str
    phone_country_code: str = "+7"
    phone_number: str = ""
    first_name: str = ""
    last_name: str = ""
    date_of_birth: str = ""
    passport_number: str = ""
    nationality: str = ""


def build_registration_profile(cfg, account: Any | None = None) -> RegistrationProfile:
    reg = cfg.registration_cfg
    defaults = reg.get("defaults") or {}
    applicant = (cfg.applicants or [{}])[0] if hasattr(cfg, "applicants") else {}
    phone_cfg = reg.get("random_phone") or {}

    def pick(name: str, default: str = "") -> str:
        for source in (reg, defaults, applicant):
            value = source.get(name) if isinstance(source, dict) else ""
            if value not in (None, ""):
                return str(value)
        return default

    email = getattr(account, "email", "") or cfg.email
    password = getattr(account, "vfs_password", "") or cfg.password
    phone_country_code = pick(
        "phone_country_code",
        str(phone_cfg.get("country_code") or "+7"),
    )
    phone_number = pick("phone_number")
    if not phone_number:
        phone_number = generate_phone_number(phone_cfg)

    return RegistrationProfile(
        email=email,
        password=password,
        phone_country_code=phone_country_code,
        phone_number=phone_number,
        first_name=pick("first_name", "IVAN"),
        last_name=pick("last_name", "IVANOV"),
        date_of_birth=pick("date_of_birth", "1990-01-01"),
        passport_number=pick("passport_number"),
        nationality=pick("nationality"),
    )


def generate_phone_number(settings: dict[str, Any] | None = None) -> str:
    settings = settings or {}
    digits = max(7, int(settings.get("digits") or 10))
    prefixes = settings.get("prefixes") or ["900", "901", "902", "903", "904", "905", "906", "909"]
    prefix = str(random.choice(prefixes))
    suffix_len = max(0, digits - len(prefix))
    suffix = "".join(str(random.randint(0, 9)) for _ in range(suffix_len))
    return (prefix + suffix)[:digits]


def register_account(sb, cfg, account: Any | None = None) -> RegistrationProfile:
    """Create and activate one VFS account using the configured mailbox."""
    profile = build_registration_profile(cfg, account)
    activate_via_email = bool(cfg.registration_cfg.get("activate_via_email", True))
    status = str(getattr(account, "status", "") or "")
    if status == "registered":
        log.info("VFS account %s is already registered; verifying login only.", _mask_email(profile.email))
        return profile
    if status == "needs_activation":
        log.info("Activating existing VFS account %s.", _mask_email(profile.email))
        _request_activation_email(sb, cfg, profile.email)
        if activate_via_email:
            _activate_from_email(sb, cfg)
        log.info("VFS account %s is activated.", _mask_email(profile.email))
        return profile

    log.info("Registering VFS account %s.", _mask_email(profile.email))

    _open_registration_entry(sb, cfg)
    human_pause(2, 4)
    _check_edge_block(sb, cfg)
    wait_out_queue(sb, cfg)
    _check_edge_block(sb, cfg)
    _pass_registration_turnstile(sb, cfg)
    _dismiss_cookie_banner(sb)

    register_url = f"{cfg.login_url.rsplit('/', 1)[0]}/register"
    _prepare_route_turnstile_stub(sb, cfg, register_url)
    _open_registration_form(sb, cfg)
    _pass_registration_turnstile(sb, cfg)
    _dismiss_cookie_banner(sb)
    _fill_registration_form(sb, cfg, profile)
    _submit_registration(sb, cfg)

    try:
        _handle_post_submit(sb, cfg)
    except RegistrationAlreadyExists:
        if activate_via_email and bool(cfg.registration_cfg.get("activate_existing", True)):
            log.info(
                "VFS says %s already exists; requesting activation email.",
                _mask_email(profile.email),
            )
            _request_activation_email(sb, cfg, profile.email)
        else:
            raise
    error_text = _visible_error_text(sb, timeout=2)
    if error_text and _text_contains_any(error_text, S.REGISTER_ALREADY_EXISTS_TEXTS):
        if activate_via_email and bool(cfg.registration_cfg.get("activate_existing", True)):
            log.info(
                "VFS says %s already exists; requesting activation email.",
                _mask_email(profile.email),
            )
            _request_activation_email(sb, cfg, profile.email)
        else:
            raise RegistrationAlreadyExists(error_text)
    elif error_text and not _looks_registration_successful(sb):
        raise RegistrationError(f"Registration rejected: {error_text}")

    if activate_via_email:
        _activate_from_email(sb, cfg)
    elif not _looks_registration_successful(sb):
        raise RegistrationError("Registration submitted, but VFS did not show a success state.")

    log.info("VFS account %s is registered.", _mask_email(profile.email))
    return profile


def check_registration_form_ready(sb, cfg) -> dict[str, Any]:
    """Open the registration UI and report whether the expected form is usable.

    This smoke test does not fill fields, submit registration, solve challenges,
    fetch OTPs, or activate email links.
    """
    result: dict[str, Any] = {
        "ready": False,
        "url": "",
        "reason": "",
        "missing": [],
    }
    try:
        _open_registration_entry(sb, cfg)
        human_pause(1, 2)
        _dismiss_cookie_banner(sb)
        entry_state = _registration_page_state(sb, cfg)
        if entry_state.get("hasTurnstile"):
            result.update(_registration_form_state(sb, cfg))
            result["reason"] = "Cloudflare/Turnstile challenge is visible; manual action is required."
            return _with_registration_screenshot(sb, cfg, result, "registration_challenge")

        _open_registration_form(sb, cfg)
        human_pause(1, 2)
        _dismiss_cookie_banner(sb)
        result.update(_registration_form_state(sb, cfg))

        required = {
            "email": result.get("email_visible"),
            "password": result.get("password_visible"),
            "confirm_password": result.get("confirm_password_visible"),
            "submit": result.get("submit_visible"),
        }
        missing = [name for name, visible in required.items() if not visible]
        result["missing"] = missing
        result["ready"] = not missing
        if missing:
            result["reason"] = "Registration form opened, but required controls are missing."
        else:
            result["reason"] = "Registration form is loaded and required controls are visible."
        return _with_registration_screenshot(sb, cfg, result, "registration_form_ready")
    except Exception as e:
        result.update(_registration_form_state(sb, cfg))
        result["reason"] = str(e)
        return _with_registration_screenshot(sb, cfg, result, "registration_form_not_ready")


def _open_registration_form(sb, cfg) -> None:
    if _registration_form_visible(sb, timeout=2):
        return

    clicked = _click_register_link(sb)
    if clicked:
        human_pause(2, 4)
    if _registration_form_visible(sb, timeout=8):
        return

    base = cfg.login_url.rsplit("/", 1)[0]
    target_path = "/" + "/".join(base.split("/")[3:]) + "/register"
    try:
        sb.execute_script(
            """
            window.history.pushState({}, '', arguments[0]);
            window.dispatchEvent(new PopStateEvent('popstate', {state: window.history.state}));
            """,
            target_path,
        )
        human_pause(2, 4)
        if _registration_form_visible(sb, timeout=6):
            return
    except Exception as e:
        log.debug("Angular registration route trigger failed: %s", e)

    for suffix in ("register", "signup", "sign-up"):
        url = f"{base}/{suffix}"
        log.debug("Trying VFS registration route: %s", url)
        _navigate_without_wait(sb, url)
        human_pause(2, 4)
        if _registration_form_visible(sb, timeout=5):
            return

    screenshot(sb, cfg.screenshot_dir, "registration_link_not_found", cfg.screenshots_enabled)
    raise RegistrationError("Could not open the VFS registration form.")


def _open_registration_entry(sb, cfg) -> None:
    log.info("Opening %s for registration.", cfg.login_url)
    try:
        _open_login_page(sb, cfg)
    except LoginError as e:
        log.debug("UC login-page open failed for registration, trying CDP navigation: %s", e)
        if not _cdp_navigate_nowait(sb, cfg, cfg.login_url):
            raise RegistrationError("Could not navigate to the VFS login page.") from e

    deadline = time.time() + max(20, int(getattr(cfg, "page_load_timeout", 35)))
    while time.time() < deadline:
        state = _registration_page_state(sb, cfg)
        if state.get("edgeBlocked"):
            _check_edge_block(sb, cfg)
        if state.get("hasLogin") or state.get("hasRegister") or state.get("hasRegisterForm"):
            return
        if state.get("hasTurnstile"):
            return
        time.sleep(0.5)

    if _cdp_send(cfg, "Page.stopLoading", {}) is None:
        _stop_page_loading(sb)
    state = _registration_page_state(sb, cfg)
    if not (
        state.get("hasLogin")
        or state.get("hasRegister")
        or state.get("hasRegisterForm")
        or state.get("hasTurnstile")
    ):
        screenshot(sb, cfg.screenshot_dir, "registration_login_load_timeout", cfg.screenshots_enabled)
        raise RegistrationError(
            "VFS login page stayed as an empty Angular shell; site assets did not load."
        )


def _cdp_navigate_nowait(sb, cfg, url: str) -> bool:
    if _cdp_send(cfg, "Page.navigate", {"url": url}) is not None:
        return True
    driver = getattr(sb, "driver", None)
    if driver and hasattr(driver, "execute_cdp_cmd"):
        try:
            driver.execute_cdp_cmd("Page.navigate", {"url": url})
            return True
        except Exception as e:
            log.debug("CDP Page.navigate failed for registration: %s", e)
    try:
        sb.execute_script("window.location.href = arguments[0];", url)
        return True
    except Exception as e:
        log.debug("JS location navigation failed for registration: %s", e)
        return False


def _registration_page_state(sb, cfg) -> dict[str, Any]:
    state = _cdp_eval(cfg, _REGISTRATION_STATE_JS)
    if isinstance(state, dict):
        return state
    try:
        state = sb.execute_script(_REGISTRATION_STATE_JS)
        return state if isinstance(state, dict) else {}
    except Exception as e:
        log.debug("Could not read registration page state: %s", e)
        return {}


_REGISTRATION_STATE_JS = """
return (() => {
  const text = (document.body && document.body.innerText || '');
  return {
    readyState: document.readyState,
    bodyTextLen: text.trim().length,
    hasLogin: !!document.querySelector(
      'input[formcontrolname="username"], input[type="email"], input[name="username"]'
    ),
    hasRegister: Array.from(document.querySelectorAll('a,button,[role="button"]')).some(el => {
      const t = (el.innerText || el.textContent || '').toLowerCase();
      return ['create account', 'sign up', 'register', 'new user', 'зарегистр', 'создать']
        .some(term => t.includes(term));
    }),
    hasRegisterForm: !!document.querySelector(
      'input[formcontrolname="emailid"], input[formcontrolname="confirmPassword"]'
    ),
    hasTurnstile: !!document.querySelector(
      'iframe[src*="challenges.cloudflare.com"], input[name="cf-turnstile-response"]'
    ),
    edgeBlocked: /403201|429201|access denied|account blocked|аккаунт заблокирован|sorry, you have been blocked/i.test(text)
  };
})();
"""


def _cdp_eval(cfg, expression: str):
    wrapped = expression.strip()
    if wrapped.startswith("return "):
        wrapped = wrapped[len("return "):].rstrip(";")
    result = _cdp_send(
        cfg,
        "Runtime.evaluate",
        {
            "expression": wrapped,
            "returnByValue": True,
            "awaitPromise": True,
        },
    )
    if not isinstance(result, dict):
        return None
    return result.get("result", {}).get("value")


def _cdp_send(cfg, method: str, params: dict[str, Any] | None = None):
    port = int(getattr(cfg, "remote_debug_port", 0) or 0)
    if not port:
        return None
    try:
        import websocket

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=3) as response:
            pages = json.loads(response.read().decode("utf-8"))
        if not pages:
            return None
        ws_url = pages[0].get("webSocketDebuggerUrl")
        if not ws_url:
            return None
        ws = websocket.create_connection(
            ws_url,
            timeout=5,
            origin=f"http://127.0.0.1:{port}",
        )
        ws.settimeout(5)
        try:
            ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
            while True:
                msg = json.loads(ws.recv())
                if msg.get("id") == 1:
                    return msg.get("result", {})
        finally:
            ws.close()
    except Exception as e:
        log.debug("CDP %s failed: %s", method, e)
        return None


def _registration_form_visible(sb, timeout: float = 4.0) -> bool:
    return bool(first_visible(sb, S.REGISTER_EMAIL, timeout=timeout))


def _click_register_link(sb) -> bool:
    link = first_visible(sb, S.REGISTER_LINK, timeout=8)
    if link:
        sb.click(link, by=by_of(link))
        return True

    try:
        return bool(sb.execute_script(
            """
            const terms = ['create account', 'create an account', 'sign up',
              'register', 'new user', 'у меня нет аккаунта', 'нет аккаунта',
              'зарегистр', 'создать'];
            const items = Array.from(document.querySelectorAll('a,button,[role="button"]'));
            const el = items.find(node => {
              const text = (node.innerText || node.textContent || '').trim().toLowerCase();
              return terms.some(term => text.includes(term));
            });
            if (!el) return false;
            el.click();
            return true;
            """
        ))
    except Exception:
        return False


def _registration_form_state(sb, cfg) -> dict[str, Any]:
    state = _registration_page_state(sb, cfg)

    def match(selectors: list[str]) -> tuple[bool, str]:
        sel = first_visible(sb, selectors, timeout=1)
        return bool(sel), sel or ""

    email_visible, email_selector = match(S.REGISTER_EMAIL)
    password_visible, password_selector = match(S.REGISTER_PASSWORD)
    confirm_visible, confirm_selector = match(S.REGISTER_CONFIRM_PASSWORD)
    phone_visible, phone_selector = match(S.REGISTER_PHONE)
    submit_visible, submit_selector = match(S.REGISTER_SUBMIT)

    checkboxes: dict[str, bool] = {}
    for control in S.REGISTER_CHECKBOX_CONTROLS:
        checkboxes[control] = _form_control_present(sb, control)

    try:
        url = sb.get_current_url()
    except Exception:
        url = ""
    try:
        title = sb.get_title()
    except Exception:
        title = ""
    try:
        body_sample = sb.execute_script(
            "return (document.body && document.body.innerText || '').trim().slice(0, 300);"
        ) or ""
    except Exception:
        body_sample = ""

    return {
        "url": url,
        "title": title,
        "body_text_len": int(state.get("bodyTextLen") or 0),
        "body_sample": body_sample,
        "has_turnstile": bool(state.get("hasTurnstile")),
        "edge_blocked": bool(state.get("edgeBlocked")),
        "email_visible": email_visible,
        "email_selector": email_selector,
        "password_visible": password_visible,
        "password_selector": password_selector,
        "confirm_password_visible": confirm_visible,
        "confirm_password_selector": confirm_selector,
        "phone_visible": phone_visible,
        "phone_selector": phone_selector,
        "submit_visible": submit_visible,
        "submit_selector": submit_selector,
        "checkboxes_present": checkboxes,
    }


def _form_control_present(sb, control: str) -> bool:
    try:
        return bool(sb.execute_script(
            "return !!document.querySelector(`[formcontrolname=\"${arguments[0]}\"]`);",
            control,
        ))
    except Exception:
        return False


def _with_registration_screenshot(sb, cfg, result: dict[str, Any], name: str) -> dict[str, Any]:
    shot = screenshot(sb, cfg.screenshot_dir, name, cfg.screenshots_enabled)
    if shot:
        result["screenshot"] = str(shot)
    return result


def _pass_registration_turnstile(sb, cfg) -> None:
    try:
        _pass_turnstile(sb, cfg)
    except LoginError as e:
        raise RegistrationError(str(e)) from e


def _prepare_route_turnstile_stub(sb, cfg, page_url: str) -> bool:
    token = _solve_route_turnstile_token(sb, cfg, page_url)
    if not token:
        return False
    _install_turnstile_stub(sb, token)
    return True


def _solve_route_turnstile_token(sb, cfg, page_url: str) -> str:
    if not cfg.captcha_enabled:
        return ""
    try:
        solver = get_solver(cfg)
    except CaptchaError as e:
        raise RegistrationError(str(e)) from e
    if solver is None:
        return ""
    sitekey = extract_turnstile_sitekey(sb) or str(
        cfg.raw.get("captcha", {}).get("turnstile_sitekey") or VFS_TURNSTILE_SITEKEY
    )
    attempts = max(1, int(cfg.registration_cfg.get("captcha_retries") or 3))
    last_error: CaptchaError | None = None
    for attempt in range(1, attempts + 1):
        try:
            log.info(
                "Preparing route Turnstile token for %s (attempt %d/%d).",
                page_url,
                attempt,
                attempts,
            )
            return solver.solve_turnstile(sitekey, page_url)
        except CaptchaError as e:
            last_error = e
            log.warning("Route Turnstile solve failed: %s", e)
    raise RegistrationError(f"Could not prepare route Turnstile token: {last_error}")


def _install_turnstile_stub(sb, token: str) -> None:
    try:
        sb.execute_script(
            """
            const token = arguments[0];
            window.__vfsTurnstileToken = token;
            const stub = {
              render(container, params = {}) {
                try {
                  const el = typeof container === 'string'
                    ? document.querySelector(container)
                    : container;
                  if (el && el.setAttribute) el.setAttribute('data-vfs-turnstile-stub', 'true');
                } catch (e) {}
                setTimeout(() => {
                  try {
                    if (typeof params.callback === 'function') params.callback(token);
                  } catch (e) {}
                }, 50);
                return 'vfs-stub-' + Date.now();
              },
              getResponse() { return token; },
              isExpired() { return false; },
              reset() {},
              remove() {},
            };
            window.__vfsTurnstileStub = stub;
            window.turnstile = stub;
            """,
            token,
        )
    except Exception as e:
        raise RegistrationError(f"Could not install Turnstile route stub: {e}") from e


def _registration_turnstile_token_present(sb) -> bool:
    try:
        return bool(sb.execute_script(
            """
            return !!(
              window.__vfsTurnstileToken ||
              document.querySelector('input[name="cf-turnstile-response"]')?.value ||
              document.querySelector('input[name="g-recaptcha-response"]')?.value ||
              document.querySelector('textarea[name="g-recaptcha-response"]')?.value
            );
            """
        ))
    except Exception:
        return False


_REGISTRATION_FORM_SYNC_JS = r"""return (() => {
const token = String(window.__vfsTurnstileToken
  || document.querySelector('input[name="cf-turnstile-response"]')?.value
  || document.querySelector('input[name="g-recaptcha-response"]')?.value
  || document.querySelector('textarea[name="g-recaptcha-response"]')?.value
  || '');
const state = {tokenLength: token.length, formsSynced: 0, captchaSynced: false, checkboxSynced: 0};

function setNativeValue(el, value) {
  if (!el) return false;
  try {
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    if (setter) setter.call(el, value);
    else el.value = value;
    for (const name of ['input', 'change', 'blur']) el.dispatchEvent(new Event(name, {bubbles: true}));
    return true;
  } catch (e) {
    return false;
  }
}

function setControl(control, value) {
  if (!control) return false;
  try {
    if (typeof control.setValue === 'function') control.setValue(value, {emitEvent: true});
    else if (typeof control.patchValue === 'function') control.patchValue(value, {emitEvent: true});
    else control.value = value;
  } catch (e) {
    try { control.setValue(value); } catch (_e) { try { control.value = value; } catch (__e) {} }
  }
  try { if (typeof control.markAsDirty === 'function') control.markAsDirty(); } catch (e) {}
  try { if (typeof control.markAsTouched === 'function') control.markAsTouched(); } catch (e) {}
  try { if (typeof control.updateValueAndValidity === 'function') control.updateValueAndValidity({emitEvent: true}); } catch (e) {}
  return true;
}

function domValue(selectors) {
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (el && String(el.value || '').trim()) return String(el.value || '');
  }
  return '';
}

const values = {
  emailid: domValue(['input[formcontrolname="emailid"]', 'input[type="email"]']),
  password: domValue(['input[formcontrolname="password"]', 'input[type="password"]']),
  confirmPassword: domValue(['input[formcontrolname="confirmPassword"]']),
  dialCode: domValue(['input[formcontrolname="dialCode"]', 'input[formcontrolname="dailcode"]']),
  contact: domValue(['input[formcontrolname="contact"]', 'input[formcontrolname="contactNumber"]', 'input[type="tel"]']),
};

function syncForm(form, owner = null) {
  if (!form || !form.controls) return false;
  const controls = form.controls;
  let synced = false;
  for (const [name, value] of Object.entries(values)) {
    if (value && controls[name]) synced = setControl(controls[name], value) || synced;
  }
  for (const name of ['isTermsChecked', 'isConsent', 'isTncChecked', 'isPrivacyPolicy', 'isDataTransfer', 'termsAndConditions']) {
    if (controls[name]) {
      synced = setControl(controls[name], true) || synced;
      state.checkboxSynced += 1;
    }
  }
  if (token && controls.captcha_api_key) {
    synced = setControl(controls.captcha_api_key, token) || synced;
    state.captchaSynced = true;
  }
  if (token && controls.captcha_version) {
    let version = 'cloudflare';
    try { version = owner?.captchaVersionConst?.cloudflare || owner?.currentCaptcha || version; } catch (e) {}
    synced = setControl(controls.captcha_version, version) || synced;
    state.captchaSynced = true;
  }
  try { if (typeof form.markAllAsTouched === 'function') form.markAllAsTouched(); } catch (e) {}
  try { if (typeof form.updateValueAndValidity === 'function') form.updateValueAndValidity({emitEvent: true}); } catch (e) {}
  if (synced) state.formsSynced += 1;
  return synced;
}

function syncCaptcha(obj) {
  if (!obj || !token) return;
  for (const name of ['submitV2Captcha', 'recaptchaCallback', 'captchaSuccess']) {
    try {
      if (typeof obj[name] === 'function') {
        obj[name](token);
        state.captchaSynced = true;
      }
    } catch (e) {}
  }
  for (const name of ['v2CaptchaSet', 'recaptchaSuccess']) {
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
  syncCaptcha(obj);
  try { if (obj.registerForm) syncForm(obj.registerForm, obj); } catch (e) {}
  try { syncForm(obj, obj); } catch (e) {}
  try {
    const control = obj.control || obj._control || obj.formControl || null;
    const name = String(obj.name || obj._name || obj.formControlName || obj.ngControlName || '');
    if (control && token && name === 'captcha_api_key') {
      if (setControl(control, token)) state.captchaSynced = true;
    }
    if (control && token && name === 'captcha_version') {
      if (setControl(control, 'cloudflare')) state.captchaSynced = true;
    }
  } catch (e) {}
  let keys = [];
  try { keys = Object.keys(obj).slice(0, 60); } catch (e) { return; }
  for (const key of keys) {
    let value;
    try { value = obj[key]; } catch (e) { continue; }
    if (!value || typeof value !== 'object') continue;
    if (value.controls) syncForm(value, obj);
    else if (depth < 2) scan(value, depth + 1, seen);
  }
}

if (token) {
  window.__vfsTurnstileToken = token;
  try { if (typeof window.recaptchaCallback === 'function') window.recaptchaCallback(token); } catch (e) {}
  try { (window.__vfsTurnstileParams || []).forEach(item => item && item.callback && item.callback(token)); } catch (e) {}
  document.querySelectorAll(
    'input[name="cf-turnstile-response"], #cf-turnstile-response, input[name="g-recaptcha-response"], textarea[name="g-recaptcha-response"]'
  ).forEach(el => setNativeValue(el, token));
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
const seen = new WeakSet();
for (const root of roots.slice(0, 300)) scan(root, 0, seen);
return state;
})();"""


def _sync_registration_angular_form(sb) -> dict[str, Any]:
    try:
        state = sb.execute_script(_REGISTRATION_FORM_SYNC_JS)
    except Exception as e:
        log.debug("Could not sync registration Angular form: %s", e)
        return {}
    return state if isinstance(state, dict) else {}


def _fill_registration_form(sb, cfg, profile: RegistrationProfile) -> None:
    _fill_required(sb, S.REGISTER_EMAIL, profile.email, "registration email")
    _fill_required(sb, S.REGISTER_PASSWORD, profile.password, "registration password")
    _fill_required(
        sb,
        S.REGISTER_CONFIRM_PASSWORD,
        profile.password,
        "registration password confirmation",
    )

    _fill_optional(sb, S.REGISTER_FIRST_NAME, profile.first_name, "first name")
    _fill_optional(sb, S.REGISTER_LAST_NAME, profile.last_name, "last name")
    _fill_optional(sb, S.REGISTER_DOB, profile.date_of_birth, "date of birth", date_value=True)
    _fill_optional(sb, S.REGISTER_PASSPORT_NUMBER, profile.passport_number, "passport number")
    _fill_select_optional(sb, S.REGISTER_NATIONALITY_SELECT, profile.nationality, "nationality")
    _fill_select_optional(
        sb,
        S.REGISTER_DIAL_CODE,
        profile.phone_country_code.lstrip("+"),
        "phone country code",
    )
    _fill_optional(sb, S.REGISTER_PHONE, profile.phone_number, "phone number")

    for control in S.REGISTER_CHECKBOX_CONTROLS:
        _set_checkbox(sb, control)

    _pass_registration_turnstile(sb, cfg)
    _sync_registration_angular_form(sb)


def _wait_for_loader_to_disappear(sb, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    log.debug("Waiting for VFS loading overlay to disappear...")
    while time.time() < deadline:
        try:
            visible = sb.execute_script("""
                const overlays = Array.from(document.querySelectorAll('.ngx-overlay, .loading-foreground, .foreground-closing'));
                return overlays.some(el => el.offsetWidth > 0 && el.offsetHeight > 0);
            """)
            if not visible:
                log.debug("VFS loading overlay disappeared.")
                return
        except Exception:
            return
        time.sleep(0.5)
    log.warning("VFS loading overlay did not disappear after %ds.", timeout)


def _fill_required(sb, selectors: list[str], value: str, label: str) -> None:
    _wait_for_loader_to_disappear(sb)
    if not _fill_optional(sb, selectors, value, label, required=True):
        raise RegistrationError(f"Could not find required {label} field.")


def _fill_optional(
    sb,
    selectors: list[str],
    value: str,
    label: str,
    *,
    date_value: bool = False,
    required: bool = False,
) -> bool:
    _wait_for_loader_to_disappear(sb)
    if not value and not required:
        return False
    sel = first_visible(sb, selectors, timeout=2 if required else 1)
    if not sel:
        return False
    text = _normalise_date(sb, sel, value) if date_value else str(value)
    _clear_and_type(sb, sel, text)
    human_pause(0.2, 0.6)
    log.debug("Filled registration field: %s.", label)
    return True


def _fill_select_optional(sb, selectors: list[str], value: str, label: str) -> bool:
    _wait_for_loader_to_disappear(sb)
    if not value:
        return False
    sel = first_visible(sb, selectors, timeout=1)
    if not sel:
        return False

    try:
        tag = (sb.execute_script(
            "return arguments[0].tagName.toLowerCase();",
            _first_element(sb, sel),
        ) or "").lower()
    except Exception:
        tag = ""

    try:
        if tag == "select":
            sb.select_option_by_text(sel, value, by=by_of(sel))
        elif "mat-select" in sel:
            sb.click(sel, by=by_of(sel))
            human_pause(0.3, 0.8)
            if not _click_option(sb, value):
                raise RegistrationError(f"Could not select {label}: {value}")
        else:
            _clear_and_type(sb, sel, value)
            _press_enter(sb, sel)
        log.debug("Filled registration select: %s.", label)
        return True
    except Exception as e:
        log.debug("Could not fill registration select %s: %s", label, e)
        return False


def _first_element(sb, sel: str):
    matches = sb.find_elements(sel, by=by_of(sel))
    visible = [el for el in matches if el.is_displayed()]
    return visible[0] if visible else (matches[0] if matches else None)


def _clear_and_type(sb, sel: str, text: str) -> None:
    try:
        from selenium.webdriver.common.keys import Keys

        el = _first_element(sb, sel)
        if el:
            el.click()
            el.send_keys(Keys.CONTROL + "a")
            el.send_keys(Keys.BACKSPACE)
            try:
                el.clear()
            except Exception:
                pass
            el.send_keys(text)
            _sync_input_value(sb, el, text)
            return
    except Exception as e:
        log.debug("Direct registration typing failed for %s: %s", sel, e)
    sb.clear(sel, by=by_of(sel))
    sb.type(sel, text, by=by_of(sel))
    el = _first_element(sb, sel)
    if el:
        _sync_input_value(sb, el, text)


def _sync_input_value(sb, element, text: str) -> None:
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
        log.debug("Could not sync input value with Angular: %s", e)


def _press_enter(sb, sel: str) -> None:
    try:
        from selenium.webdriver.common.keys import Keys

        el = _first_element(sb, sel)
        if el:
            el.send_keys(Keys.ENTER)
    except Exception:
        try:
            sb.send_keys(sel, "\n", by=by_of(sel))
        except Exception:
            pass


def _normalise_date(sb, sel: str, value: str) -> str:
    text = str(value)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    try:
        input_type = (sb.get_attribute(sel, "type", by=by_of(sel)) or "").lower()
    except Exception:
        input_type = ""
    if input_type == "date":
        return text
    return datetime.strptime(text, "%Y-%m-%d").strftime("%d%m%Y")


def _click_option(sb, value: str) -> bool:
    literal = xpath_literal(value)
    xpaths = [
        f'//mat-option//span[normalize-space()={literal}]',
        f'//mat-option[normalize-space()={literal}]',
        f'//*[@role="option"][normalize-space()={literal}]',
        f'//mat-option//span[contains(normalize-space(), {literal})]',
        f'//*[@role="option"][contains(normalize-space(), {literal})]',
    ]
    for xp in xpaths:
        try:
            if sb.is_element_present(xp, by="xpath"):
                sb.click(xp, by="xpath")
                return True
        except Exception:
            continue
    return False


def _set_checkbox(sb, control: str) -> bool:
    try:
        _wait_for_loader_to_disappear(sb)
        return bool(sb.execute_script(
            """
            const control = arguments[0];
            const host = document.querySelector(`[formcontrolname="${control}"]`);
            if (!host) return false;

            const input = host.matches('input') ? host : host.querySelector('input[type="checkbox"]');
            if (!input) return false;

            if (input.checked) return true;

            // Try native click on checkbox input first
            input.click();

            // Fallback click on container
            if (!input.checked) {
                const clickable = host.closest('mat-checkbox') || host;
                clickable.click();
            }

            // Manual fallback with trusted events
            if (!input.checked) {
                input.checked = true;
                input.dispatchEvent(new Event('input', { bubbles: true }));
                input.dispatchEvent(new Event('change', { bubbles: true }));
            }

            return input.checked;
            """,
            control,
        ))
    except Exception as e:
        log.debug("Could not set registration checkbox %s: %s", control, e)
        return False


def _submit_registration(sb, cfg) -> None:
    _wait_for_loader_to_disappear(sb)
    _ensure_lift_api_clearance(sb, cfg, "https://lift-api.vfsglobal.com/user/registration")
    deadline = time.time() + 45
    last_error: Exception | None = None
    while time.time() < deadline:
        _pass_registration_turnstile(sb, cfg)
        _wait_for_loader_to_disappear(sb)
        if _registration_page_state(sb, cfg).get("hasTurnstile") and not _registration_turnstile_token_present(sb):
            human_pause(0.8, 1.5)
            continue
        btn = first_visible(sb, S.REGISTER_SUBMIT, timeout=3)
        if not btn:
            human_pause(0.4, 0.9)
            continue
        try:
            # Click via JavaScript only after Angular has enabled the button. Do not
            # force-enable invalid forms because VFS may count invalid submits.
            sync_state = _sync_registration_angular_form(sb)
            log.debug("Registration Angular sync before submit: %s", sync_state)
            clicked = sb.execute_script("""
                const visible = el => !!el && el.offsetWidth > 0 && el.offsetHeight > 0;
                const enabled = el => !(el.disabled || el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true');
                const labels = ['register', 'create', 'submit', 'continue', 'зарегистр', 'созда', 'продолж'];
                const buttons = Array.from(document.querySelectorAll('button'));
                const preferred = buttons.find(el => {
                    const text = (el.innerText || el.textContent || '').trim().toLowerCase();
                    return visible(el) && enabled(el) && labels.some(label => text.includes(label));
                });
                if (preferred) {
                    preferred.scrollIntoView({block: 'center', inline: 'center'});
                    preferred.click();
                    return true;
                }
                const selectors = ["#trigger", "button[type='submit']", "button[id*='submit']"];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el && visible(el) && enabled(el)) {
                        el.scrollIntoView({block: 'center', inline: 'center'});
                        el.click();
                        return true;
                    }
                }
                return false;
            """)
            if clicked:
                log.info("Clicked registration submit button via JavaScript.")
                human_pause(1, 2)
                _confirm_captcha_modal(sb)
                human_pause(3, 6)
                if _registration_submit_accepted(sb, cfg):
                    return
                human_pause(1, 2)
                continue
        except RegistrationError:
            raise
        except Exception as e:
            log.debug("Could not click registration submit button via JS: %s", e)
        try:
            sync_state = _sync_registration_angular_form(sb)
            log.debug("Registration Angular sync before normal submit: %s", sync_state)
            disabled = sb.get_attribute(btn, "disabled", by=by_of(btn))
            if disabled not in (None, "false"):
                human_pause(0.8, 1.5)
                continue
        except Exception:
            pass
        try:
            sb.click(btn, by=by_of(btn))
            human_pause(1, 2)
            _confirm_captcha_modal(sb)
            human_pause(3, 6)
            if _registration_submit_accepted(sb, cfg):
                return
            human_pause(1, 2)
        except RegistrationError:
            raise
        except Exception as e:
            last_error = e
            human_pause(0.4, 0.9)
    screenshot(sb, cfg.screenshot_dir, "registration_submit_failed", cfg.screenshots_enabled)
    reason = str(last_error) if last_error else "submit button stayed disabled or unavailable"
    raise RegistrationError(f"Could not submit the registration form: {reason}")


def _registration_submit_accepted(sb, cfg, timeout: float = 6.0) -> bool:
    deadline = time.time() + timeout
    last_error_text = ""
    while time.time() < deadline:
        _check_edge_block(sb, cfg)
        if _looks_registration_successful(sb):
            return True
        error_text = _visible_error_text(sb, timeout=0.2)
        if error_text:
            last_error_text = error_text
            if _text_contains_any(error_text, S.REGISTER_ALREADY_EXISTS_TEXTS):
                return True
            raise RegistrationError(f"Registration rejected: {error_text}")
        if first_visible(sb, S.OTP_INPUT, timeout=0.2):
            return True
        if not _registration_form_visible(sb, timeout=0.2):
            return True
        time.sleep(0.4)
    if last_error_text:
        log.debug("Registration submit produced validation text: %s", last_error_text)
    log.debug("Registration submit did not change the form; retrying.")
    return False


def _handle_post_submit(sb, cfg) -> None:
    _check_edge_block(sb, cfg)
    wait_out_queue(sb, cfg)
    _check_edge_block(sb, cfg)

    otp_sel = first_visible(sb, S.OTP_INPUT, timeout=5)
    if otp_sel:
        log.info("Registration OTP requested.")
        try:
            code = get_otp(cfg, prompt="Enter the VFS registration OTP")
        except OTPError as e:
            raise RegistrationError(str(e)) from e
        if not fill_otp_into_page(sb, code, S.OTP_INPUT, S.OTP_SUBMIT):
            raise RegistrationError("Could not enter the registration OTP.")
        human_pause(3, 6)

    text = _visible_error_text(sb, timeout=2)
    if text and _text_contains_any(text, S.REGISTER_ALREADY_EXISTS_TEXTS):
        raise RegistrationAlreadyExists(text)
    if text and not _looks_registration_successful(sb):
        raise RegistrationError(f"Registration rejected: {text}")


def _wait_for_angular_boot(sb, cfg, timeout: float = 30) -> None:
    """Wait until the Angular SPA has bootstrapped (login form or register link visible)."""
    deadline = time.time() + timeout
    log.debug("Waiting for Angular SPA to bootstrap...")
    while time.time() < deadline:
        state = _registration_page_state(sb, cfg)
        if (
            state.get("hasLogin")
            or state.get("hasRegister")
            or state.get("hasRegisterForm")
            or (state.get("readyState") == "complete" and int(state.get("bodyTextLen") or 0) > 200)
        ):
            log.debug("Angular SPA bootstrapped.")
            return
        time.sleep(0.5)
    log.warning("Angular SPA did not fully bootstrap within %ds.", timeout)


def _wait_for_activation_form(sb, cfg, timeout: float = 30) -> None:
    """Trigger Angular routing to /email-activation and wait for the form to appear.

    Uses history.pushState + popstate so Angular's router processes the new path
    without a full page reload (which would clear the bootstrapped SPA).
    """
    base = cfg.login_url.rsplit("/", 1)[0]
    target_path = "/".join(base.split("/")[3:])  # e.g. rus/ru/svn
    activation_path = f"/{target_path}/email-activation"

    _FORM_CHECK_JS = """
    const el = document.querySelector(
      'input[formcontrolname="emailid"], input[formcontrolname="email"], input[type="email"].form-control'
    );
    return !!el;
    """

    _ROUTER_TRIGGER_JS = """
    (function(path) {
      try {
        window.history.pushState({}, '', path);
        window.dispatchEvent(new PopStateEvent('popstate', {state: window.history.state}));
      } catch(e) {}
    })(arguments[0]);
    """

    deadline = time.time() + timeout
    last_trigger = 0.0
    log.debug("Triggering Angular router to %s", activation_path)
    while time.time() < deadline:
        # Re-fire Angular router trigger every 3 seconds
        if time.time() - last_trigger >= 3:
            try:
                sb.execute_script(_ROUTER_TRIGGER_JS, activation_path)
            except Exception as e:
                log.debug("Router trigger error: %s", e)
            last_trigger = time.time()

        try:
            found = sb.execute_script(_FORM_CHECK_JS)
        except Exception:
            found = False
        if found:
            log.debug("Activation email form rendered successfully.")
            return
        time.sleep(0.5)

    log.warning("Activation form did not appear within %ds after router triggers.", timeout)


def _request_activation_email(sb, cfg, email: str) -> None:
    url = f"{cfg.login_url.rsplit('/', 1)[0]}/email-activation"
    log.info("Opening VFS email-activation page for %s.", _mask_email(email))

    # Land on the login page first so Angular fully initialises
    if not _cdp_navigate_nowait(sb, cfg, cfg.login_url):
        raise RegistrationError("Could not navigate to the VFS login page for activation.")
    _wait_for_angular_boot(sb, cfg, timeout=30)
    _dismiss_cookie_banner(sb)

    _prepare_route_turnstile_stub(sb, cfg, url)
    # Trigger Angular router to /email-activation (pushState done inside _wait_for_activation_form)
    _wait_for_activation_form(sb, cfg, timeout=35)

    _check_edge_block(sb, cfg)
    wait_out_queue(sb, cfg)
    _check_edge_block(sb, cfg)
    _pass_registration_turnstile(sb, cfg)
    _dismiss_cookie_banner(sb)
    _wait_for_loader_to_disappear(sb)

    email_sel = first_visible(sb, S.ACTIVATION_EMAIL, timeout=12)
    if not email_sel:
        screenshot(sb, cfg.screenshot_dir, "activation_email_form_missing", cfg.screenshots_enabled)
        raise RegistrationError("Could not find the VFS email-activation form.")
    _clear_and_type(sb, email_sel, email)
    try:
        sb.execute_script(
            """
            const el = document.querySelector(
              'input[formcontrolname="emailid"], input[formcontrolname="email"], input[type="email"]'
            );
            if (el) {
              el.dispatchEvent(new Event('input', { bubbles: true }));
              el.dispatchEvent(new Event('change', { bubbles: true }));
            }
            """
        )
    except Exception as e:
        log.debug("Could not dispatch activation email events: %s", e)
    human_pause(0.5, 1.2)
    _pass_registration_turnstile(sb, cfg)
    _wait_for_loader_to_disappear(sb)
    _ensure_lift_api_clearance(sb, cfg, "https://lift-api.vfsglobal.com/user/resendactivationmail")

    submit_sel = first_visible(sb, S.ACTIVATION_SUBMIT, timeout=12)
    if not submit_sel:
        screenshot(sb, cfg.screenshot_dir, "activation_submit_missing", cfg.screenshots_enabled)
        raise RegistrationError("Could not find the VFS email-activation submit button.")
    try:
        sb.click(submit_sel, by=by_of(submit_sel))
    except Exception as e:
        log.debug("Normal activation submit click failed: %s", e)
        clicked = sb.execute_script(
            """
            const buttons = Array.from(document.querySelectorAll('button'));
            const btn = buttons.find(el => {
              const text = (el.innerText || el.textContent || '').trim().toLowerCase();
              return text.includes('activate') || text.includes('актив') || text.includes('send') || text.includes('submit');
            });
            if (!btn) return false;
            btn.click();
            return true;
            """
        )
        if not clicked:
            screenshot(sb, cfg.screenshot_dir, "activation_submit_failed", cfg.screenshots_enabled)
            raise RegistrationError("Could not submit the VFS email-activation form.") from e
    human_pause(1, 2)
    _confirm_captcha_modal(sb)
    human_pause(3, 6)
    _check_edge_block(sb, cfg)
    text = _visible_error_text(sb, timeout=2)
    if text:
        raise RegistrationError(f"VFS email activation request rejected: {text}")


def _activate_from_email(sb, cfg) -> None:
    href_contains = str(cfg.registration_cfg.get("activation_link_contains") or "activateemail")
    search = str(cfg.registration_cfg.get("activation_email_search") or "VFS")
    wait_seconds = int(cfg.registration_cfg.get("activation_wait_seconds") or 120)
    log.info("Waiting for VFS activation email.")
    try:
        link = get_email_link(
            cfg,
            href_contains=href_contains,
            search=search,
            wait_seconds=wait_seconds,
        )
    except OTPError as e:
        raise RegistrationError(str(e)) from e
    if not link:
        screenshot(sb, cfg.screenshot_dir, "activation_email_not_found", cfg.screenshots_enabled)
        raise RegistrationError("Registration submitted, but no activation email link was found.")

    log.info("Opening VFS activation link from mailbox.")
    _navigate_without_wait(sb, link)
    human_pause(4, 7)
    _check_edge_block(sb, cfg)
    wait_out_queue(sb, cfg)
    _check_edge_block(sb, cfg)

    if _looks_activation_completed(sb):
        return
    if _activation_redirected_to_login(sb):
        log.info("Activation link redirected to login; final activation will be verified by dashboard login.")
        return
    screenshot(sb, cfg.screenshot_dir, "activation_unclear", cfg.screenshots_enabled)
    raise RegistrationError("Opened activation link, but VFS did not show activation success.")


def _confirm_captcha_modal(sb, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _captcha_modal_visible(sb):
            return False
        for sel in (
            '//mat-dialog-container//button[contains(., "Submit") or contains(., "Confirm") or contains(., "Continue") or contains(., "Подтверд") or contains(., "Продолж")]',
            '//*[contains(@class, "cdk-overlay-pane")]//button[contains(., "Submit") or contains(., "Confirm") or contains(., "Continue") or contains(., "Подтверд") or contains(., "Продолж")]',
            '//*[@role="dialog"]//button[contains(., "Submit") or contains(., "Confirm") or contains(., "Continue") or contains(., "Подтверд") or contains(., "Продолж")]',
        ):
            btn = first_visible(sb, [sel], timeout=0.5)
            if not btn:
                continue
            try:
                sb.click(btn, by=by_of(btn))
                human_pause(1, 2)
                if not _captcha_modal_visible(sb):
                    log.info("Confirmed VFS captcha modal.")
                    return True
            except Exception as e:
                log.debug("Normal captcha modal click failed: %s", e)
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
                    if (!visible(el) || el.disabled) return false;
                    const label = (el.innerText || el.textContent || '').trim().toLowerCase();
                    return ['submit', 'confirm', 'continue', 'подтвердить', 'продолжить'].some(t => label.includes(t));
                  });
                  if (btn) {
                    btn.click();
                    return (btn.innerText || btn.textContent || '').trim() || true;
                  }
                }
                return '';
                """
            )
        except Exception:
            clicked = ""
        if clicked:
            human_pause(1, 2)
            if not _captcha_modal_visible(sb):
                log.info("Confirmed VFS captcha modal.")
                return True
        time.sleep(0.5)
    if _captcha_modal_visible(sb):
        raise RegistrationError("VFS captcha confirmation modal did not close after clicking Submit.")
    return False


def _ensure_lift_api_clearance(sb, cfg, website_url: str) -> None:
    if not cfg.captcha_enabled:
        return
    optional = "resendactivationmail" in website_url.lower()
    if not (getattr(cfg, "captcha_proxy", "") or "").strip():
        log.warning(
            "Skipping pre-solved Cloudflare clearance for %s because no remote captcha.proxy is configured. "
            "Local VPN proxies such as 127.0.0.1 are not reachable by CapSolver.",
            website_url,
        )
        return
    try:
        solved = solve_cloudflare_clearance(sb, cfg, website_url)
    except CaptchaError as e:
        if optional:
            log.warning("Continuing without Cloudflare clearance for optional VFS API %s: %s", website_url, e)
            return
        raise RegistrationError(f"Could not solve Cloudflare challenge for VFS API: {e}") from e
    if not solved:
        if optional:
            log.warning("Continuing without Cloudflare clearance for optional VFS API %s.", website_url)
            return
        raise RegistrationError(f"Could not get Cloudflare clearance for VFS API: {website_url}")


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


def _looks_registration_successful(sb) -> bool:
    return _has_text(sb, S.REGISTER_SUCCESS_TEXTS + S.REGISTER_ACTIVATED_TEXTS)


def _visible_error_text(sb, timeout: float = 2.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            text = sb.execute_script(
                """
                const selectors = 'mat-error,.error-message,.errorMessage,.alert-danger,.alert-error,.c-brand-error';
                const errors = Array.from(document.querySelectorAll(selectors))
                  .filter(el => el.offsetWidth > 0 && el.offsetHeight > 0)
                  .map(el => (el.innerText || el.textContent || '').trim())
                  .filter(text => text && text !== '*');
                return errors[0] || '';
                """
            )
            if text:
                return str(text).strip()
        except Exception:
            pass
        time.sleep(0.25)
    return ""


def _looks_login_or_success_page(sb) -> bool:
    try:
        url = (sb.get_current_url() or "").lower()
    except Exception:
        url = ""
    if "/login" in url:
        return True
    return bool(first_visible(sb, S.LOGIN_EMAIL, timeout=2)) or _looks_registration_successful(sb)


def _looks_activation_completed(sb) -> bool:
    success_texts = [
        "account activated",
        "email verified",
        "email has been verified",
        "activation successful",
        "activated successfully",
        "аккаунт активирован",
        "учетная запись активирована",
        "учётная запись активирована",
        "электронная почта подтверждена",
        "email подтвержден",
    ]
    return bool(page_has_any_text(sb, success_texts))


def _activation_redirected_to_login(sb) -> bool:
    try:
        url = (sb.get_current_url() or "").lower()
    except Exception:
        url = ""
    return "/login" in url or bool(first_visible(sb, S.LOGIN_EMAIL, timeout=2))


def _has_text(sb, texts: list[str]) -> bool:
    return bool(page_has_any_text(sb, texts))


def _text_contains_any(text: str, needles: list[str]) -> bool:
    low = text.lower()
    return any(needle.lower() in low for needle in needles)


def _mask_email(email: str) -> str:
    if "@" not in email:
        return "<account>"
    name, domain = email.split("@", 1)
    return f"{name[:2]}***@{domain}"

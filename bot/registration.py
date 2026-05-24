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
from .login import (
    _check_edge_block,
    _dismiss_cookie_banner,
    _navigate_without_wait,
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
    log.info("Registering VFS account %s.", _mask_email(profile.email))

    _open_registration_entry(sb, cfg)
    human_pause(2, 4)
    _check_edge_block(sb, cfg)
    wait_out_queue(sb, cfg)
    _check_edge_block(sb, cfg)
    _pass_turnstile(sb, cfg)
    _dismiss_cookie_banner(sb)

    _open_registration_form(sb, cfg)
    _pass_turnstile(sb, cfg)
    _dismiss_cookie_banner(sb)
    _fill_registration_form(sb, cfg, profile)
    _submit_registration(sb, cfg)

    _handle_post_submit(sb, cfg)
    if _has_text(sb, S.REGISTER_ALREADY_EXISTS_TEXTS):
        raise RegistrationAlreadyExists("VFS says this email is already registered.")

    activate_via_email = bool(cfg.registration_cfg.get("activate_via_email", True))
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
    if not _cdp_navigate_nowait(sb, cfg, cfg.login_url):
        raise RegistrationError("Could not navigate to the VFS login page.")

    deadline = time.time() + max(20, int(getattr(cfg, "page_load_timeout", 35)))
    while time.time() < deadline:
        state = _registration_page_state(sb, cfg)
        if state.get("edgeBlocked"):
            _check_edge_block(sb, cfg)
        if state.get("hasLogin") or state.get("hasRegister") or state.get("hasRegisterForm"):
            return
        if state.get("hasTurnstile"):
            return
        if state.get("readyState") == "complete" and int(state.get("bodyTextLen") or 0) > 0:
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
    edgeBlocked: /403201|access denied|sorry, you have been blocked/i.test(text)
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

    _pass_turnstile(sb, cfg)


def _fill_required(sb, selectors: list[str], value: str, label: str) -> None:
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
            return
    except Exception as e:
        log.debug("Direct registration typing failed for %s: %s", sel, e)
    sb.clear(sel, by=by_of(sel))
    sb.type(sel, text, by=by_of(sel))


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
    xpaths = [
        f'//mat-option//span[normalize-space()="{value}"]',
        f'//mat-option[normalize-space()="{value}"]',
        f'//*[@role="option"][normalize-space()="{value}"]',
        f'//mat-option//span[contains(normalize-space(), "{value}")]',
        f'//*[@role="option"][contains(normalize-space(), "{value}")]',
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
        return bool(sb.execute_script(
            """
            const control = arguments[0];
            const host = document.querySelector(`[formcontrolname="${control}"]`);
            if (!host) return false;
            const input = host.matches('input') ? host : host.querySelector('input');
            if (input && input.checked) return true;
            const clickable = host.closest('mat-checkbox') || host;
            clickable.click();
            if (input && !input.checked) {
              input.checked = true;
              input.dispatchEvent(new Event('input', {bubbles: true}));
              input.dispatchEvent(new Event('change', {bubbles: true}));
            }
            return true;
            """,
            control,
        ))
    except Exception as e:
        log.debug("Could not set registration checkbox %s: %s", control, e)
        return False


def _submit_registration(sb, cfg) -> None:
    deadline = time.time() + 45
    last_error: Exception | None = None
    while time.time() < deadline:
        _pass_turnstile(sb, cfg)
        btn = first_visible(sb, S.REGISTER_SUBMIT, timeout=3)
        if not btn:
            human_pause(0.4, 0.9)
            continue
        try:
            disabled = sb.get_attribute(btn, "disabled", by=by_of(btn))
            if disabled not in (None, "false"):
                human_pause(0.8, 1.5)
                continue
        except Exception:
            pass
        try:
            sb.click(btn, by=by_of(btn))
            human_pause(3, 6)
            return
        except Exception as e:
            last_error = e
            human_pause(0.4, 0.9)
    screenshot(sb, cfg.screenshot_dir, "registration_submit_failed", cfg.screenshots_enabled)
    raise RegistrationError(f"Could not submit the registration form: {last_error}")


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

    err = first_visible(sb, S.REGISTER_ERROR, timeout=2)
    if err:
        try:
            text = sb.get_text(err, by=by_of(err)).strip()
        except Exception:
            text = ""
        if text and _text_contains_any(text, S.REGISTER_ALREADY_EXISTS_TEXTS):
            raise RegistrationAlreadyExists(text)
        if text and not _looks_registration_successful(sb):
            raise RegistrationError(f"Registration rejected: {text}")


def _activate_from_email(sb, cfg) -> None:
    if _has_text(sb, S.REGISTER_ACTIVATED_TEXTS):
        return
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

    if _has_text(sb, S.REGISTER_ACTIVATED_TEXTS) or _looks_login_or_success_page(sb):
        return
    screenshot(sb, cfg.screenshot_dir, "activation_unclear", cfg.screenshots_enabled)
    raise RegistrationError("Opened activation link, but VFS did not show activation success.")


def _looks_registration_successful(sb) -> bool:
    return _has_text(sb, S.REGISTER_SUCCESS_TEXTS + S.REGISTER_ACTIVATED_TEXTS)


def _looks_login_or_success_page(sb) -> bool:
    try:
        url = (sb.get_current_url() or "").lower()
    except Exception:
        url = ""
    if "/login" in url:
        return True
    return bool(first_visible(sb, S.LOGIN_EMAIL, timeout=2)) or _looks_registration_successful(sb)


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

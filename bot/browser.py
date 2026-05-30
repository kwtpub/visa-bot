"""Create and configure the SeleniumBase UC-mode browser.

UC mode = SeleniumBase's "undetected Chrome" driver. It's what gives us a
fighting chance against Cloudflare Turnstile on the VFS login page. The bot
does not use SeleniumBase GUI captcha click helpers because they move the real
Windows mouse.

We use the SB() context-manager form so the bot is a plain script (no pytest).
"""
from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .proxy_bridge import auth_bridge_supported, start_proxy_auth_bridge
from .util import log


class SeleniumDriverAdapter:
    """Small SeleniumBase-like wrapper for an already-running Chrome driver."""

    def __init__(self, driver):
        self.driver = driver
        self._default_timeout = 8

    def set_default_timeout(self, seconds: int) -> None:
        self._default_timeout = seconds

    def open(self, url: str) -> None:
        self.driver.get(url)

    def uc_open_with_reconnect(self, url: str, reconnect_seconds: int = 0) -> None:
        self.driver.get(url)

    def get_current_url(self) -> str:
        return self.driver.current_url

    def get_title(self) -> str:
        return self.driver.title

    def get_page_source(self) -> str:
        return self.driver.page_source

    def execute_script(self, script: str, *args):
        return self.driver.execute_script(script, *args)

    def refresh(self) -> None:
        self.driver.refresh()

    def find_elements(self, selector: str, by: str = "css selector"):
        return self.driver.find_elements(self._by(by), selector)

    def is_element_present(self, selector: str, by: str = "css selector") -> bool:
        return bool(self.find_elements(selector, by=by))

    def is_element_visible(self, selector: str, by: str = "css selector") -> bool:
        return any(el.is_displayed() for el in self.find_elements(selector, by=by))

    def is_text_visible(self, text: str) -> bool:
        try:
            body = self.driver.find_element(self._by("css selector"), "body")
            return text in (body.text or "")
        except Exception:
            return text in (self.driver.page_source or "")

    def click(self, selector: str, by: str = "css selector") -> None:
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        locator = (self._by(by), selector)
        try:
            wait = WebDriverWait(self.driver, self._default_timeout)
            el = wait.until(EC.element_to_be_clickable(locator))
        except Exception:
            matches = self.find_elements(selector, by=by)
            visible = [el for el in matches if el.is_displayed()]
            if not visible:
                raise
            el = visible[0]
        try:
            self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
        except Exception:
            pass
        try:
            el.click()
        except Exception:
            self.driver.execute_script("arguments[0].click();", el)

    def clear(self, selector: str, by: str = "css selector") -> None:
        self.driver.find_element(self._by(by), selector).clear()

    def type(self, selector: str, text: str, by: str = "css selector") -> None:
        self.driver.find_element(self._by(by), selector).send_keys(text)

    def send_keys(self, selector: str, text: str, by: str = "css selector") -> None:
        self.driver.find_element(self._by(by), selector).send_keys(text)

    def scroll_to(self, selector: str, by: str = "css selector") -> None:
        el = self.driver.find_element(self._by(by), selector)
        self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)

    def press_keys(self, selector: str, keys: str, by: str = "css selector") -> None:
        from selenium.webdriver.common.keys import Keys

        value = Keys.ESCAPE if keys in {"\ue00c", "ESCAPE", "Escape", "оЂЊ"} else keys
        self.driver.find_element(self._by(by), selector).send_keys(value)

    def get_text(self, selector: str, by: str = "css selector") -> str:
        return self.driver.find_element(self._by(by), selector).text

    def get_attribute(self, selector: str, name: str, by: str = "css selector") -> str | None:
        return self.driver.find_element(self._by(by), selector).get_attribute(name)

    def select_option_by_text(self, selector: str, text: str, by: str = "css selector") -> None:
        from selenium.webdriver.support.select import Select

        Select(self.driver.find_element(self._by(by), selector)).select_by_visible_text(text)

    def save_screenshot(self, path: str) -> bool:
        return self.driver.save_screenshot(path)

    @staticmethod
    def _by(by: str):
        from selenium.webdriver.common.by import By

        return By.XPATH if by == "xpath" else By.CSS_SELECTOR


def _proxy_for_sb(proxy: str) -> str | None:
    """Normalise the proxy for SeleniumBase.

    SeleniumBase wants 'user:pass@host:port' for HTTP proxies, but it parses
    and REQUIRES the scheme for SOCKS proxies (socks5://, socks4://) so Chrome
    is configured as a SOCKS proxy rather than HTTP. So strip only http(s)://;
    keep socks schemes. (socks5h:// -> socks5://: SB doesn't know the 'h' form,
    and proxy-side DNS is the default for Chrome's SOCKS5 anyway.)
    """
    proxy = (proxy or "").strip()
    if not proxy:
        return None
    if proxy.lower().startswith("socks5h://"):
        return "socks5://" + proxy[len("socks5h://"):]
    for scheme in ("http://", "https://"):
        if proxy.lower().startswith(scheme):
            return proxy[len(scheme):]
    return proxy


@contextmanager
def open_browser(cfg) -> Iterator[object]:
    """Yield a ready SeleniumBase `sb` object in UC mode.

    Usage:
        with open_browser(cfg) as sb:
            sb.uc_open_with_reconnect(cfg.login_url, 4)
            ...
    """
    debugger_address = getattr(cfg, "debugger_address", "")
    if debugger_address:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options

        log.info("Attaching to existing Chrome at %s", debugger_address)
        options = Options()
        options.add_experimental_option("debuggerAddress", debugger_address)
        driver = webdriver.Chrome(options=options)
        try:
            adapter = SeleniumDriverAdapter(driver)
            adapter.set_default_timeout(8)
            yield adapter
        finally:
            log.info("Leaving attached Chrome session open.")
        return

    _patch_tempfile_permissions()
    from seleniumbase import SB

    upstream_proxy = _proxy_for_sb(cfg.proxy)
    proxy = upstream_proxy
    bridge = None
    if (
        upstream_proxy
        and getattr(cfg, "proxy_auth_bridge_enabled", True)
        and auth_bridge_supported(upstream_proxy)
    ):
        bridge = start_proxy_auth_bridge(upstream_proxy)
        if bridge:
            bridge.__enter__()
            proxy = bridge.proxy
    kwargs = dict(
        uc=True,                       # undetected Chrome
        headless=cfg.headless,
        # locale/timezone help blend in; tweak for your applicant country
        locale_code="en",
        # uc_cdp / incognito off — keep it simple and stable
        incognito=False,
        # block obvious automation flags via SB's UC patches (automatic)
    )
    chromium_args: list[str] = []
    user_data_dir = _user_data_dir(cfg)
    if user_data_dir:
        kwargs["user_data_dir"] = str(user_data_dir)
    remote_debug_port = getattr(cfg, "remote_debug_port", 0)
    if remote_debug_port:
        chromium_args.append(f"--remote-debugging-port={remote_debug_port}")
        chromium_args.append("--remote-allow-origins=*")
        log.info("Chrome remote debugging enabled on localhost:%s", remote_debug_port)
    if getattr(cfg, "background_browser", False) and not cfg.headless:
        x, y = getattr(cfg, "browser_window_position", (-32000, 0))
        width, height = getattr(cfg, "browser_window_size", (1280, 900))
        chromium_args.append(f"--window-position={x},{y}")
        chromium_args.append(f"--window-size={width},{height}")
        log.info("Browser background mode enabled at position %s,%s.", x, y)
    if proxy:
        kwargs["proxy"] = proxy
        if bridge:
            log.info("Using proxy %s via local auth bridge %s", _redact(upstream_proxy), proxy)
        else:
            log.info("Using proxy %s", _redact(proxy))
    else:
        log.warning("No proxy configured — expect possible Cloudflare 403 on hosting IPs.")
    if cfg.chrome_version:
        # SeleniumBase accepts e.g. binary_location or driver version pinning;
        # the simplest knob that works across versions:
        log.info("(chrome_version='%s' requested — pin via `sbase get chromedriver %s` if SB picks wrong)",
                 cfg.chrome_version, cfg.chrome_version)
    if chromium_args:
        kwargs["chromium_arg"] = chromium_args

    log.info("Launching browser (UC mode, headless=%s)…", cfg.headless)
    try:
        with SB(**kwargs) as sb:
            # A sane default wait so first_present/visible aren't fighting SB's own.
            try:
                sb.set_default_timeout(8)
            except Exception:
                pass
            try:
                sb.driver.set_page_load_timeout(int(getattr(cfg, "page_load_timeout", 35)))
            except Exception as e:
                log.debug("Could not set page load timeout: %s", e)
            try:
                sb.driver.set_script_timeout(15)
            except Exception as e:
                log.debug("Could not set script timeout: %s", e)
            _apply_background_window(sb, cfg)
            try:
                # Register Turnstile hook at the CDP level to ensure it runs before any VFS Global scripts load.
                # This guarantees that turnstile.render is captured and resolved callbacks are triggered correctly.
                from .captcha import turnstile_hook_source_for_cdp
                sb.driver.execute_cdp_cmd(
                    "Page.addScriptToEvaluateOnNewDocument",
                    {"source": turnstile_hook_source_for_cdp()},
                )
                log.info("Registered Turnstile CDP hook on new document.")
            except Exception as e:
                log.debug("Could not register Turnstile CDP hook: %s", e)
            yield sb
    finally:
        if bridge:
            bridge.__exit__(None, None, None)


def _redact(proxy: str) -> str:
    if "@" in proxy:
        creds, host = proxy.split("@", 1)
        user = creds.split(":", 1)[0]
        return f"{user}:***@{host}"
    return proxy


def _apply_background_window(sb, cfg) -> None:
    if not getattr(cfg, "background_browser", False) or getattr(cfg, "headless", False):
        return
    try:
        x, y = getattr(cfg, "browser_window_position", (-32000, 0))
        width, height = getattr(cfg, "browser_window_size", (1280, 900))
        sb.driver.set_window_size(width, height)
        sb.driver.set_window_position(x, y)
        log.debug("Moved browser window to background position %s,%s (%sx%s).", x, y, width, height)
    except Exception as e:
        log.debug("Could not move browser window to background position: %s", e)


def _user_data_dir(cfg) -> Path | None:
    configured = str(cfg.raw.get("network", {}).get("user_data_dir") or "").strip()
    if not configured:
        return None
    path = Path(configured)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def _patch_tempfile_permissions() -> None:
    """Use Windows-compatible permissions for temp Chrome profiles."""
    if os.name != "nt" or getattr(tempfile, "_vfsbot_mkdtemp_patched", False):
        return

    def mkdtemp(suffix=None, prefix=None, dir=None):
        prefix, suffix, dir, output_type = tempfile._sanitize_params(prefix, suffix, dir)
        names = tempfile._get_candidate_names()
        if output_type is bytes:
            names = map(os.fsencode, names)
        for _ in range(tempfile.TMP_MAX):
            name = next(names)
            path = os.path.join(dir, prefix + name + suffix)
            try:
                os.mkdir(path, 0o777)
            except FileExistsError:
                continue
            except PermissionError:
                if os.path.isdir(dir):
                    continue
                raise
            return os.path.abspath(path)
        raise FileExistsError("No usable temporary directory name found")

    tempfile.mkdtemp = mkdtemp
    tempfile._vfsbot_mkdtemp_patched = True

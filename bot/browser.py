"""Create and configure the SeleniumBase UC-mode browser.

UC mode = SeleniumBase's "undetected Chrome" driver. It's what gives us a
fighting chance against Cloudflare Turnstile on the VFS login page
(`sb.uc_gui_click_captcha()` / `sb.uc_open_with_reconnect()`).

We use the SB() context-manager form so the bot is a plain script (no pytest).
"""
from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

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
    """SeleniumBase wants the proxy as 'user:pass@host:port' or 'host:port'."""
    proxy = (proxy or "").strip()
    if not proxy:
        return None
    # strip a scheme if the user pasted one
    for scheme in ("http://", "https://", "socks5://", "socks5h://"):
        if proxy.lower().startswith(scheme):
            proxy = proxy[len(scheme):]
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
            try:
                driver.service.stop()
            except Exception:
                pass
        return

    _patch_tempfile_permissions()
    from seleniumbase import SB

    proxy = _proxy_for_sb(cfg.proxy)
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
    if proxy:
        kwargs["proxy"] = proxy
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
    with SB(**kwargs) as sb:
        # A sane default wait so first_present/visible aren't fighting SB's own.
        try:
            sb.set_default_timeout(8)
        except Exception:
            pass
        yield sb


def _redact(proxy: str) -> str:
    if "@" in proxy:
        creds, host = proxy.split("@", 1)
        user = creds.split(":", 1)[0]
        return f"{user}:***@{host}"
    return proxy


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

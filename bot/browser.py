"""Create and configure the SeleniumBase UC-mode browser.

UC mode = SeleniumBase's "undetected Chrome" driver. It's what gives us a
fighting chance against Cloudflare Turnstile on the VFS login page
(`sb.uc_gui_click_captcha()` / `sb.uc_open_with_reconnect()`).

We use the SB() context-manager form so the bot is a plain script (no pytest).
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from seleniumbase import SB

from .util import log


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
    if proxy:
        kwargs["proxy"] = proxy
        log.info("Using proxy %s", _redact(proxy))
    else:
        log.warning("No proxy configured — expect possible Cloudflare 403 on hosting IPs.")
    if cfg.chrome_version:
        # SeleniumBase accepts e.g. binary_location or driver version pinning;
        # the simplest knob that works across versions:
        kwargs["chromium_arg"] = ""  # placeholder; version pin via env if needed
        log.info("(chrome_version='%s' requested — pin via `sbase get chromedriver %s` if SB picks wrong)",
                 cfg.chrome_version, cfg.chrome_version)

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

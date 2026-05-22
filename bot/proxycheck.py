"""Proxy health check before launching a browser session."""
from __future__ import annotations

import re

import requests

from .util import log


class ProxyDead(RuntimeError):
    """Raised when the configured browser proxy cannot pass a basic check."""


def _proxy_url(proxy: str) -> str:
    proxy = (proxy or "").strip()
    if not proxy:
        return ""
    if re.match(r"^[a-z][a-z0-9+.-]*://", proxy, flags=re.I):
        return proxy
    return f"http://{proxy}"


def _redact_proxy(proxy: str) -> str:
    proxy = (proxy or "").strip()
    match = re.match(r"^([a-z][a-z0-9+.-]*://)?([^/@:]+)(?::[^/@]*)?@(.+)$", proxy, flags=re.I)
    if match:
        scheme = match.group(1) or ""
        return f"{scheme}{match.group(2)}:***@{match.group(3)}"
    return proxy


def _mask_ip(ip: str) -> str:
    parts = str(ip).split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return f"{parts[0]}.xxx.xxx.{parts[3]}"
    return str(ip)[:32]


def precheck_proxy(cfg) -> None:
    """Fail fast if the configured proxy is unusable.

    The check is skipped when attaching to an already-open Chrome because that
    browser may have been started with a different proxy outside this process.
    """
    proxy = getattr(cfg, "proxy", "")
    if not proxy:
        return
    if not getattr(cfg, "proxy_precheck_enabled", True):
        log.info("Proxy precheck disabled by config.")
        return
    if getattr(cfg, "debugger_address", ""):
        log.info("Skipping proxy precheck while attached to existing Chrome.")
        return

    url = getattr(cfg, "proxy_check_url", "") or "https://api.ipify.org?format=json"
    timeout = getattr(cfg, "proxy_check_timeout", 20)
    proxied = _proxy_url(proxy)
    proxies = {"http": proxied, "https": proxied}
    log.info("Checking proxy %s via %s", _redact_proxy(proxied), url)
    try:
        resp = requests.get(url, proxies=proxies, timeout=timeout)
    except requests.RequestException as e:
        raise ProxyDead(f"PROXY_DEAD: {e}") from e

    if resp.status_code != 200:
        snippet = (resp.text or "").strip().replace("\n", " ")[:160]
        raise ProxyDead(f"PROXY_DEAD: check returned HTTP {resp.status_code}: {snippet}")

    ip = ""
    try:
        data = resp.json()
        ip = str(data.get("ip") or "")
    except ValueError:
        ip = (resp.text or "").strip()
    log.info("Proxy precheck OK%s", f" (exit IP {_mask_ip(ip)})" if ip else "")

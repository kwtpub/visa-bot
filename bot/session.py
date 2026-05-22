"""Import/export browser state for reusing a manually logged-in VFS session."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .util import log


def portal_origin(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError(f"Invalid portal URL: {url!r}")
    return f"{parsed.scheme}://{parsed.netloc}/"


def _storage_dump_script(storage_name: str) -> str:
    return (
        f"const out = {{}};"
        f"for (let i = 0; i < {storage_name}.length; i++) {{"
        f"  const k = {storage_name}.key(i); out[k] = {storage_name}.getItem(k);"
        f"}}"
        f"return out;"
    )


def _storage_restore_script(storage_name: str, values: dict[str, str]) -> str:
    return (
        f"const values = {json.dumps(values)};"
        f"for (const [k, v] of Object.entries(values)) {{"
        f"  {storage_name}.setItem(k, v);"
        f"}}"
        f"return Object.keys(values).length;"
    )


def _normalise_cookie(cookie: dict[str, Any], *, with_domain: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": str(cookie["name"]),
        "value": str(cookie.get("value", "")),
        "path": cookie.get("path") or "/",
    }
    if with_domain and cookie.get("domain"):
        out["domain"] = str(cookie["domain"])
    if cookie.get("secure") is not None:
        out["secure"] = bool(cookie["secure"])
    if cookie.get("expiry") is not None:
        try:
            out["expiry"] = int(cookie["expiry"])
        except (TypeError, ValueError):
            pass
    same_site = cookie.get("sameSite")
    if same_site in {"Strict", "Lax", "None"}:
        out["sameSite"] = same_site
    if out["name"].startswith("__Host-"):
        out.pop("domain", None)
        out["path"] = "/"
        out["secure"] = True
    return out


def _add_cookie(driver, cookie: dict[str, Any]) -> bool:
    if not cookie.get("name"):
        return False
    try:
        driver.add_cookie(_normalise_cookie(cookie, with_domain=True))
        return True
    except Exception as first_error:
        try:
            driver.add_cookie(_normalise_cookie(cookie, with_domain=False))
            return True
        except Exception:
            log.debug("Could not restore cookie %r: %s", cookie.get("name"), first_error)
            return False


def load_browser_state(sb, cfg, path: Path) -> bool:
    """Load cookies and web storage into the current browser.

    Returns True when at least one cookie/storage item was restored.
    """
    if not path.exists():
        log.info("Session state file not found: %s", path)
        return False

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Could not read session state from %s: %s", path, e)
        return False

    if isinstance(raw, list):
        cookies = raw
        local_storage: dict[str, str] = {}
        session_storage: dict[str, str] = {}
    else:
        cookies = raw.get("cookies") or []
        local_storage = raw.get("local_storage") or {}
        session_storage = raw.get("session_storage") or {}

    origin = portal_origin(cfg.login_url)
    try:
        sb.open(origin)
    except Exception as e:
        log.debug("Could not open portal origin before restoring state: %s", e)

    restored_cookies = 0
    driver = getattr(sb, "driver", None)
    if driver is not None:
        for cookie in cookies:
            if isinstance(cookie, dict) and _add_cookie(driver, cookie):
                restored_cookies += 1

    restored_storage = 0
    for storage_name, values in (
        ("localStorage", local_storage),
        ("sessionStorage", session_storage),
    ):
        if not isinstance(values, dict) or not values:
            continue
        try:
            restored_storage += int(sb.execute_script(_storage_restore_script(storage_name, values)) or 0)
        except Exception as e:
            log.debug("Could not restore %s: %s", storage_name, e)

    restored = restored_cookies + restored_storage
    log.info(
        "Restored session state from %s: %d cookie(s), %d storage item(s).",
        path,
        restored_cookies,
        restored_storage,
    )
    return restored > 0


def save_browser_state(sb, cfg, path: Path) -> bool:
    """Save current cookies and web storage to a local JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        cookies = sb.driver.get_cookies()
    except Exception as e:
        log.warning("Could not read browser cookies: %s", e)
        cookies = []

    local_storage: dict[str, str] = {}
    session_storage: dict[str, str] = {}
    for storage_name in ("localStorage", "sessionStorage"):
        try:
            values = sb.execute_script(_storage_dump_script(storage_name)) or {}
        except Exception as e:
            log.debug("Could not dump %s: %s", storage_name, e)
            values = {}
        if storage_name == "localStorage":
            local_storage = values
        else:
            session_storage = values

    try:
        current_url = sb.get_current_url() or ""
    except Exception:
        current_url = ""

    state = {
        "version": 1,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "url": current_url,
        "origin": portal_origin(cfg.login_url),
        "cookies": cookies,
        "local_storage": local_storage,
        "session_storage": session_storage,
    }
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    log.info(
        "Saved session state to %s: %d cookie(s), %d localStorage, %d sessionStorage.",
        path,
        len(cookies),
        len(local_storage),
        len(session_storage),
    )
    return bool(cookies or local_storage or session_storage)

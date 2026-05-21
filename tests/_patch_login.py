"""Patch _page_looks_blank_or_error and _open_login_page with retry logic."""
import pathlib
import re

p = pathlib.Path("bot/login.py")
content = p.read_text(encoding="utf-8")

# Replace _page_looks_blank_or_error with improved version
old_blank = '''def _page_looks_blank_or_error(sb) -> bool:
    """Detect the empty Chrome error document that UC reconnect can leave behind."""
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
    compact = "".join(src.split())
    return len(src) < 200 and compact in {
        "<html><head></head><body></body></html>",
        "<html><head></head><body></body></html>",
    }'''

new_blank = '''def _page_looks_blank_or_error(sb) -> bool:
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
    return len(src) < 200 and compact in {
        "<html><head></head><body></body></html>",
        "<html><head></head><body></body></html>",
    }'''

if old_blank not in content:
    # Try with \r\n
    old_blank_crlf = old_blank.replace("\n", "\r\n")
    if old_blank_crlf in content:
        content = content.replace(old_blank_crlf, new_blank.replace("\n", "\r\n"))
    else:
        print("WARNING: Could not find old _page_looks_blank_or_error")
        print("Searching for it...")
        idx = content.find("def _page_looks_blank_or_error")
        if idx >= 0:
            print(f"Found at index {idx}")
            # Find the end of the function (next def or class at the same level)
            next_def = content.find("\ndef _open_login_page", idx)
            if next_def < 0:
                next_def = content.find("\r\ndef _open_login_page", idx)
            if next_def >= 0:
                content = content[:idx] + new_blank + content[next_def:]
                print("Replaced using index-based method")
            else:
                print("Could not find end of function")
        else:
            print("Function not found at all!")
else:
    content = content.replace(old_blank, new_blank)

# Now replace _open_login_page with improved version with retries
old_open = '''def _open_login_page(sb, cfg, reconnect_seconds: int = 5) -> None:
    """Open the login page, falling back when UC reconnect produces a blank page."""
    try:
        sb.uc_open_with_reconnect(cfg.login_url, reconnect_seconds)
    except Exception as e:
        log.debug("uc_open_with_reconnect failed, using normal open: %s", e)
        sb.open(cfg.login_url)
        return

    human_pause(1.0, 2.0)
    if _page_looks_blank_or_error(sb):
        log.warning("UC reconnect loaded a blank/error page; retrying with normal browser open.")
        try:
            sb.open(cfg.login_url)
        except Exception as e:
            log.debug("normal open after UC reconnect failed: %s", e)'''

new_open = '''def _open_login_page(sb, cfg, reconnect_seconds: int = 5) -> None:
    """Open the login page with retry logic for unstable proxies.

    Tries UC reconnect first, then falls back to normal open, with up to
    3 total attempts to handle proxy connection resets.
    """
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        # Try UC reconnect first (best for Cloudflare bypass)
        try:
            sb.uc_open_with_reconnect(cfg.login_url, reconnect_seconds)
        except Exception as e:
            log.debug("uc_open_with_reconnect failed (attempt %d): %s", attempt, e)
            try:
                sb.open(cfg.login_url)
            except Exception as e2:
                log.debug("normal open also failed (attempt %d): %s", attempt, e2)
                if attempt < max_retries:
                    human_pause(2.0, 4.0)
                    continue
                return

        human_pause(1.5, 3.0)

        if _page_looks_blank_or_error(sb):
            log.warning(
                "Page blank/error after load (attempt %d/%d); retrying…",
                attempt, max_retries
            )
            # Try a simple open as fallback
            try:
                sb.open(cfg.login_url)
                human_pause(2.0, 4.0)
            except Exception as e:
                log.debug("fallback open failed: %s", e)

            if _page_looks_blank_or_error(sb):
                if attempt < max_retries:
                    human_pause(3.0, 6.0)
                    continue
                log.error("Page still blank/error after %d attempts.", max_retries)
            else:
                log.info("Page loaded successfully via fallback open.")
                return
        else:
            log.debug("Page loaded successfully (attempt %d).", attempt)
            return'''

if old_open not in content:
    old_open_crlf = old_open.replace("\n", "\r\n")
    if old_open_crlf in content:
        content = content.replace(old_open_crlf, new_open.replace("\n", "\r\n"))
    else:
        print("WARNING: Could not find old _open_login_page, trying index method")
        idx = content.find("def _open_login_page")
        if idx >= 0:
            next_def = content.find("\ndef _solve_with_paid_service", idx)
            if next_def < 0:
                next_def = content.find("\r\ndef _solve_with_paid_service", idx)
            if next_def >= 0:
                content = content[:idx] + new_open + content[next_def:]
                print("Replaced using index-based method")
        else:
            print("Function not found!")
else:
    content = content.replace(old_open, new_open)

p.write_text(content, encoding="utf-8")
print("OK - login.py patched successfully")
print(f"File size: {p.stat().st_size} bytes")

# Verify syntax
import ast
ast.parse(content)
print("Syntax check: OK")

"""Insert the two missing functions into bot/login.py."""
import pathlib

p = pathlib.Path("bot/login.py")
content = p.read_text(encoding="utf-8")

new_funcs = '''
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


def _wait_for_submit_enabled(sb, submit_sel: str, timeout: int = 15) -> None:
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
                return  # button is enabled
        except Exception:
            return  # element gone / selector stale
        time.sleep(0.5)
    log.warning(
        "Submit button still disabled after %ds - clicking anyway (Turnstile "
        "may not have completed).", timeout
    )


'''

marker = "def perform_login(sb, cfg)"
idx = content.index(marker)
content = content[:idx] + new_funcs + content[idx:]
p.write_text(content, encoding="utf-8")
print("OK - functions inserted successfully")
print(f"New file size: {p.stat().st_size} bytes")

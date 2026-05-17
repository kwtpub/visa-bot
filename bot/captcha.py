"""Cloudflare Turnstile solver via paid 3rd-party services.

Currently supports:
  * CapSolver (https://capsolver.com)  — task type AntiTurnstileTaskProxyLess

Adding 2Captcha / Anti-Captcha later is a matter of subclassing `Solver`.

Public surface:
    get_solver(cfg)                -> Solver | None
    extract_turnstile_sitekey(sb)  -> (sitekey, page_url) | (None, None)
    inject_turnstile_token(sb, t)  -> bool
"""
from __future__ import annotations

import time
from typing import Optional

import requests

from .util import log


class CaptchaError(RuntimeError):
    """Solver failed in a way the bot can't recover from."""


# ---------------------------------------------------------------------------
# Solvers
# ---------------------------------------------------------------------------
class Solver:
    """Abstract solver interface. Sub-classes implement solve_turnstile()."""

    def solve_turnstile(self, site_key: str, page_url: str,
                        action: Optional[str] = None,
                        cdata: Optional[str] = None) -> str:
        raise NotImplementedError


class CapSolver(Solver):
    """CapSolver.com — fast & cheap on Turnstile (~$0.8/1000)."""

    BASE = "https://api.capsolver.com"

    def __init__(self, api_key: str, timeout_seconds: int = 120) -> None:
        if not api_key:
            raise CaptchaError("CapSolver: empty api_key")
        self.api_key = api_key
        self.timeout = max(20, int(timeout_seconds))

    # --- balance check (handy for diagnostics) --------------------------
    def balance(self) -> Optional[float]:
        try:
            r = requests.post(
                f"{self.BASE}/getBalance",
                json={"clientKey": self.api_key},
                timeout=20,
            )
            data = r.json()
            if data.get("errorId"):
                log.warning("CapSolver getBalance error: %s", data)
                return None
            return float(data.get("balance", 0))
        except Exception as e:
            log.warning("CapSolver getBalance failed: %s", e)
            return None

    # --- main flow ------------------------------------------------------
    def solve_turnstile(self, site_key: str, page_url: str,
                        action: Optional[str] = None,
                        cdata: Optional[str] = None) -> str:
        if not site_key:
            raise CaptchaError("solve_turnstile: empty site_key")
        if not page_url:
            raise CaptchaError("solve_turnstile: empty page_url")

        task: dict = {
            "type": "AntiTurnstileTaskProxyLess",
            "websiteURL": page_url,
            "websiteKey": site_key,
        }
        meta: dict = {}
        if action:
            meta["action"] = action
        if cdata:
            meta["cdata"] = cdata
        if meta:
            task["metadata"] = meta

        # 1) createTask
        try:
            r = requests.post(
                f"{self.BASE}/createTask",
                json={"clientKey": self.api_key, "task": task},
                timeout=30,
            )
        except Exception as e:
            raise CaptchaError(f"CapSolver createTask network error: {e}") from e

        try:
            data = r.json()
        except Exception:
            raise CaptchaError(f"CapSolver createTask non-JSON response: {r.text[:200]!r}")

        if data.get("errorId"):
            raise CaptchaError(
                f"CapSolver createTask failed: errorCode={data.get('errorCode')} "
                f"desc={data.get('errorDescription')}"
            )
        task_id = data.get("taskId")
        if not task_id:
            raise CaptchaError(f"CapSolver createTask: no taskId in {data!r}")
        log.info("CapSolver task created: %s (sitekey=%s…)", task_id, site_key[:12])

        # 2) poll getTaskResult
        started = time.time()
        deadline = started + self.timeout
        delay = 2.0
        while time.time() < deadline:
            time.sleep(delay)
            delay = min(delay + 1.0, 5.0)
            try:
                r = requests.post(
                    f"{self.BASE}/getTaskResult",
                    json={"clientKey": self.api_key, "taskId": task_id},
                    timeout=30,
                )
                data = r.json()
            except Exception as e:
                log.debug("CapSolver poll transient error: %s", e)
                continue

            if data.get("errorId"):
                raise CaptchaError(
                    f"CapSolver getTaskResult failed: errorCode={data.get('errorCode')} "
                    f"desc={data.get('errorDescription')}"
                )

            status = data.get("status")
            if status == "ready":
                sol = data.get("solution") or {}
                token = sol.get("token") or sol.get("gRecaptchaResponse")
                if not token:
                    raise CaptchaError(f"CapSolver: empty token in {data!r}")
                elapsed = time.time() - started
                log.info("CapSolver SOLVED in %.1fs (token=%s…)", elapsed, token[:24])
                return token
            # status == "processing" -> keep polling

        raise CaptchaError(f"CapSolver: timed out after {self.timeout}s waiting for token")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def get_solver(cfg) -> Optional[Solver]:
    """Return a configured Solver instance, or None if disabled / misconfigured."""
    provider = (cfg.captcha_provider or "none").lower()
    if provider in ("", "none", "off", "disabled"):
        return None
    if not cfg.captcha_api_key:
        log.warning("captcha.provider=%r set but api_key is empty — solver disabled.", provider)
        return None
    if provider == "capsolver":
        return CapSolver(cfg.captcha_api_key, cfg.captcha_timeout)
    if provider in ("twocaptcha", "2captcha"):
        raise CaptchaError("2Captcha provider is not implemented yet — use 'capsolver'.")
    raise CaptchaError(f"Unknown captcha provider: {provider!r}")


# ---------------------------------------------------------------------------
# Page helpers
# ---------------------------------------------------------------------------
_FIND_SITEKEY_JS = r"""
// Look for a Turnstile widget on the page (top doc + all frames we can read).
function fromDoc(doc) {
  try {
    // Standard widget markup: <div class="cf-turnstile" data-sitekey="...">
    let el = doc.querySelector('[data-sitekey]');
    if (el && el.getAttribute('data-sitekey')) {
      return el.getAttribute('data-sitekey');
    }
    // Some pages render via <iframe src="...?k=SITEKEY">
    let ifr = doc.querySelector('iframe[src*="challenges.cloudflare.com"]');
    if (ifr) {
      let m = (ifr.getAttribute('src') || '').match(/[?&]k=([^&#]+)/);
      if (m) return decodeURIComponent(m[1]);
    }
  } catch (e) {}
  return null;
}
let sk = fromDoc(document);
if (!sk) {
  for (const f of Array.from(document.querySelectorAll('iframe'))) {
    try {
      if (f.contentDocument) {
        sk = fromDoc(f.contentDocument);
        if (sk) break;
      }
    } catch (e) {}
  }
}
return sk;
"""

_INJECT_TOKEN_JS = r"""
const token = arguments[0];
let count = 0;

// 1) all <input name="cf-turnstile-response">
document.querySelectorAll('input[name="cf-turnstile-response"]').forEach(i => {
  i.value = token;
  i.dispatchEvent(new Event('input',  {bubbles:true}));
  i.dispatchEvent(new Event('change', {bubbles:true}));
  count++;
});

// 2) call data-callback if widget defined one
document.querySelectorAll('[data-sitekey]').forEach(el => {
  const cbName = el.getAttribute('data-callback');
  if (cbName && typeof window[cbName] === 'function') {
    try { window[cbName](token); count++; } catch (e) {}
  }
});

// 3) some Angular pages keep a hidden form-field instead — try common ids/names.
document.querySelectorAll(
  '#cf-turnstile-response, input[name="g-recaptcha-response"], textarea[name="g-recaptcha-response"]'
).forEach(i => {
  i.value = token;
  i.dispatchEvent(new Event('input',  {bubbles:true}));
  i.dispatchEvent(new Event('change', {bubbles:true}));
  count++;
});
return count;
"""


def extract_turnstile_sitekey(sb) -> Optional[str]:
    """Try to find the Turnstile sitekey on the current page.

    Returns the sitekey string or None if we couldn't locate it.
    """
    try:
        sk = sb.execute_script(_FIND_SITEKEY_JS)
    except Exception as e:
        log.debug("extract_turnstile_sitekey JS error: %s", e)
        return None
    if not sk:
        return None
    sk = str(sk).strip()
    return sk or None


def inject_turnstile_token(sb, token: str) -> bool:
    """Inject a solved token into the page and fire input/change events.

    Returns True if at least one input / callback was wired up.
    """
    if not token:
        return False
    try:
        n = sb.execute_script(_INJECT_TOKEN_JS, token)
    except Exception as e:
        log.warning("inject_turnstile_token failed: %s", e)
        return False
    n = int(n or 0)
    log.info("Injected Turnstile token into %d field(s)/callback(s).", n)
    return n > 0


def page_url(sb) -> str:
    try:
        return sb.get_current_url() or ""
    except Exception:
        return ""

"""Cloudflare Turnstile solver via paid 3rd-party services.

Currently supports:
  * CapSolver (https://capsolver.com)  вЂ” task type AntiTurnstileTaskProxyLess

Adding 2Captcha / Anti-Captcha later is a matter of subclassing `Solver`.

Public surface:
    get_solver(cfg)                -> Solver | None
    extract_turnstile_sitekey(sb)  -> (sitekey, page_url) | (None, None)
    extract_turnstile_metadata(sb) -> dict
    install_turnstile_hook(sb)     -> bool
    inject_turnstile_token(sb, t)  -> bool
"""
from __future__ import annotations

import time
import re
import json
from typing import Any, Optional
from urllib.parse import urlparse

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

    def solve_cloudflare_challenge(
        self,
        website_url: str,
        proxy: str,
        user_agent: str | None = None,
        html: str | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError


class CapSolver(Solver):
    """CapSolver.com вЂ” fast & cheap on Turnstile (~$0.8/1000)."""

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
        log.info("CapSolver task created: %s (sitekey=%sвЂ¦)", task_id, site_key[:12])

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
                log.info("CapSolver SOLVED in %.1fs (token=%sвЂ¦)", elapsed, token[:24])
                return token
            # status == "processing" -> keep polling

        raise CaptchaError(f"CapSolver: timed out after {self.timeout}s waiting for token")

    def solve_cloudflare_challenge(
        self,
        website_url: str,
        proxy: str,
        user_agent: str | None = None,
        html: str | None = None,
    ) -> dict[str, Any]:
        """Solve a Cloudflare managed challenge and return cookies/userAgent."""
        if not website_url:
            raise CaptchaError("solve_cloudflare_challenge: empty website_url")
        if not proxy:
            raise CaptchaError("solve_cloudflare_challenge: empty proxy")

        task: dict[str, Any] = {
            "type": "AntiCloudflareTask",
            "websiteURL": website_url,
            "proxy": _proxy_for_capsolver(proxy),
        }
        if user_agent:
            task["userAgent"] = user_agent
        if html:
            task["html"] = html

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
        log.info("CapSolver Cloudflare task created: %s", task_id)

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
                log.debug("CapSolver Cloudflare poll transient error: %s", e)
                continue

            if data.get("errorId"):
                raise CaptchaError(
                    f"CapSolver getTaskResult failed: errorCode={data.get('errorCode')} "
                    f"desc={data.get('errorDescription')}"
                )

            if data.get("status") == "ready":
                sol = data.get("solution") or {}
                if not isinstance(sol, dict) or not sol:
                    raise CaptchaError(f"CapSolver Cloudflare: empty solution in {data!r}")
                elapsed = time.time() - started
                log.info("CapSolver Cloudflare SOLVED in %.1fs.", elapsed)
                return sol

        raise CaptchaError(
            f"CapSolver Cloudflare: timed out after {self.timeout}s waiting for clearance"
        )


def _proxy_for_capsolver(proxy: str) -> str:
    proxy = (proxy or "").strip()
    if not proxy:
        return ""
    if "://" in proxy:
        return proxy
    if "@" in proxy:
        return f"http://{proxy}"
    return proxy


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def get_solver(cfg) -> Optional[Solver]:
    """Return a configured Solver instance, or None if disabled / misconfigured."""
    provider = (cfg.captcha_provider or "none").lower()
    if provider in ("", "none", "off", "disabled"):
        return None
    if not cfg.captcha_api_key:
        log.warning("captcha.provider=%r set but api_key is empty вЂ” solver disabled.", provider)
        return None
    if provider == "capsolver":
        return CapSolver(cfg.captcha_api_key, cfg.captcha_timeout)
    if provider in ("twocaptcha", "2captcha"):
        raise CaptchaError("2Captcha provider is not implemented yet вЂ” use 'capsolver'.")
    raise CaptchaError(f"Unknown captcha provider: {provider!r}")


# ---------------------------------------------------------------------------
# Page helpers
# ---------------------------------------------------------------------------
_FIND_SITEKEY_JS = r"""return (() => {
// Look for a Turnstile widget on the page (top doc + all frames we can read).
function fromDoc(doc) {
  try {
    // Standard widget markup: <div class="cf-turnstile" data-sitekey="...">
    let el = doc.querySelector('[data-sitekey]');
    if (el && el.getAttribute('data-sitekey')) {
      return el.getAttribute('data-sitekey');
    }
    // Some pages render via <iframe src="...?k=SITEKEY">, while newer
    // Cloudflare Turnstile frames can carry the sitekey as a path segment.
    let ifr = doc.querySelector('iframe[src*="challenges.cloudflare.com"]');
    if (ifr) {
      let src = ifr.getAttribute('src') || '';
      let m = src.match(/[?&]k=([^&#]+)/);
      if (m) return decodeURIComponent(m[1]);
      m = src.match(/\/(0x[0-9A-Za-z_-]{20,})(?:[/?#]|$)/);
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
})();
"""

_INJECT_TOKEN_JS = r"""return ((token) => {
let count = 0;
const seenCallbacks = new Set();

function callCallback(cb) {
  if (typeof cb !== 'function' || seenCallbacks.has(cb)) return;
  try {
    cb(token);
    seenCallbacks.add(cb);
    count++;
  } catch (e) {}
}

window.__vfsTurnstileToken = token;
if (window.turnstile && !window.turnstile.__vfsTokenPatched) {
  const originalGetResponse = typeof window.turnstile.getResponse === 'function'
    ? window.turnstile.getResponse.bind(window.turnstile)
    : null;
  const originalIsExpired = typeof window.turnstile.isExpired === 'function'
    ? window.turnstile.isExpired.bind(window.turnstile)
    : null;
  window.turnstile.getResponse = function() {
    return window.__vfsTurnstileToken || (originalGetResponse ? originalGetResponse.apply(null, arguments) : '');
  };
  window.turnstile.isExpired = function() {
    return window.__vfsTurnstileToken ? false : (originalIsExpired ? originalIsExpired.apply(null, arguments) : false);
  };
  window.turnstile.__vfsTokenPatched = true;
  count++;
}

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
    callCallback(window[cbName]);
  }
});

// 3) some Angular pages keep a hidden form-field instead вЂ” try common ids/names.
(window.__vfsTurnstileParams || []).forEach(item => {
  callCallback(item && item.callback);
});

document.querySelectorAll(
  '#cf-turnstile-response, input[name="g-recaptcha-response"], textarea[name="g-recaptcha-response"]'
).forEach(i => {
  i.value = token;
  i.dispatchEvent(new Event('input',  {bubbles:true}));
  i.dispatchEvent(new Event('change', {bubbles:true}));
  count++;
});
return count;
})(arguments[0]);
"""

_INJECT_TOKEN_CDP_TEMPLATE = r"""(() => {
const token = __TOKEN_JSON__;
let count = 0;
const seenCallbacks = new Set();

function callCallback(cb) {
  if (typeof cb !== 'function' || seenCallbacks.has(cb)) return;
  try {
    cb(token);
    seenCallbacks.add(cb);
    count++;
  } catch (e) {}
}

window.__vfsTurnstileToken = token;
if (window.turnstile && !window.turnstile.__vfsTokenPatched) {
  const originalGetResponse = typeof window.turnstile.getResponse === 'function'
    ? window.turnstile.getResponse.bind(window.turnstile)
    : null;
  const originalIsExpired = typeof window.turnstile.isExpired === 'function'
    ? window.turnstile.isExpired.bind(window.turnstile)
    : null;
  window.turnstile.getResponse = function() {
    return window.__vfsTurnstileToken || (originalGetResponse ? originalGetResponse.apply(null, arguments) : '');
  };
  window.turnstile.isExpired = function() {
    return window.__vfsTurnstileToken ? false : (originalIsExpired ? originalIsExpired.apply(null, arguments) : false);
  };
  window.turnstile.__vfsTokenPatched = true;
  count++;
}

document.querySelectorAll('input[name="cf-turnstile-response"]').forEach(i => {
  i.value = token;
  i.dispatchEvent(new Event('input',  {bubbles:true}));
  i.dispatchEvent(new Event('change', {bubbles:true}));
  count++;
});

document.querySelectorAll('[data-sitekey]').forEach(el => {
  const cbName = el.getAttribute('data-callback');
  if (cbName && typeof window[cbName] === 'function') {
    callCallback(window[cbName]);
  }
});

(window.__vfsTurnstileParams || []).forEach(item => {
  callCallback(item && item.callback);
});

document.querySelectorAll(
  '#cf-turnstile-response, input[name="g-recaptcha-response"], textarea[name="g-recaptcha-response"]'
).forEach(i => {
  i.value = token;
  i.dispatchEvent(new Event('input',  {bubbles:true}));
  i.dispatchEvent(new Event('change', {bubbles:true}));
  count++;
});
return count;
})();
"""

_INSTALL_TURNSTILE_HOOK_JS = r"""return (() => {
if (window.__vfsTurnstileHookInstalled) {
  return true;
}
window.__vfsTurnstileHookInstalled = true;
window.__vfsTurnstileParams = window.__vfsTurnstileParams || [];

function remember(args, widgetId) {
  try {
    const params = args && args[1] ? args[1] : {};
    window.__vfsTurnstileParams.push({
      sitekey: params.sitekey || params.siteKey || null,
      action: params.action || null,
      cData: params.cData || params.cdata || null,
      chlPageData: params.chlPageData || null,
      callback: params.callback || null,
      errorCallback: params['error-callback'] || params.errorCallback || null,
      expiredCallback: params['expired-callback'] || params.expiredCallback || null,
      widgetId: widgetId || null,
    });
  } catch (e) {}
}

function patch(ts) {
  if (!ts || typeof ts.render !== 'function' || ts.render.__vfsWrapped) {
    return;
  }
  const originalRender = ts.render.bind(ts);
  const wrapped = function() {
    const args = Array.prototype.slice.call(arguments);
    const widgetId = originalRender.apply(ts, args);
    remember(args, widgetId);
    return widgetId;
  };
  wrapped.__vfsWrapped = true;
  wrapped.__vfsOriginal = originalRender;
  ts.render = wrapped;
}

patch(window.turnstile);
if (!window.__vfsTurnstileHookTimer) {
  window.__vfsTurnstileHookTimer = setInterval(() => {
    patch(window.turnstile);
    if (window.turnstile && window.turnstile.render && window.turnstile.render.__vfsWrapped) {
      clearInterval(window.__vfsTurnstileHookTimer);
      window.__vfsTurnstileHookTimer = null;
    }
  }, 50);
}
return true;
})();
"""


def extract_turnstile_sitekey(sb) -> Optional[str]:
    """Try to find the Turnstile sitekey on the current page.

    Returns the sitekey string or None if we couldn't locate it.
    """
    try:
        sk = sb.execute_script(_FIND_SITEKEY_JS)
    except Exception as e:
        log.debug("extract_turnstile_sitekey JS error: %s", e)
        sk = None
    if not sk:
        try:
            src = sb.get_page_source() or ""
        except Exception:
            src = ""
        m = re.search(r'data-sitekey=["\']([^"\']+)["\']', src, re.I)
        if not m:
            m = re.search(
                r'challenges\.cloudflare\.com[^"\']*/(0x[0-9A-Za-z_-]{20,})(?:[/?#"\']|$)',
                src,
                re.I,
            )
        if not m:
            try:
                resources = sb.execute_script(
                    "return performance.getEntriesByType('resource')"
                    ".map(e => e.name).join('\\n')"
                ) or ""
            except Exception:
                resources = ""
            m = re.search(
                r'challenges\.cloudflare\.com[^\s"\']*/(0x[0-9A-Za-z_-]{20,})(?:[/?#\s"\']|$)',
                str(resources),
                re.I,
            )
        if not m:
            return None
        sk = m.group(1)
    sk = str(sk).strip()
    return sk or None


def extract_turnstile_metadata(sb) -> dict:
    """Return the latest render parameters captured by install_turnstile_hook()."""
    try:
        data = sb.execute_script(
            """
            return (() => {
              const items = window.__vfsTurnstileParams || [];
              const item = items.length ? items[items.length - 1] : null;
              if (!item) return {};
              return {
                sitekey: item.sitekey || null,
                action: item.action || null,
                cData: item.cData || null,
                chlPageData: item.chlPageData || null,
              };
            })();
            """
        )
    except Exception as e:
        log.debug("extract_turnstile_metadata failed: %s", e)
        return {}
    return data if isinstance(data, dict) else {}


def install_turnstile_hook(sb) -> bool:
    """Capture Turnstile render parameters before a widget/dialog appears."""
    try:
        return bool(sb.execute_script(_INSTALL_TURNSTILE_HOOK_JS))
    except Exception as e:
        log.debug("install_turnstile_hook failed: %s", e)
        return False


def inject_turnstile_token(sb, token: str) -> bool:
    """Inject a solved token into the page and fire input/change events.

    Returns True if at least one input / callback was wired up.
    """
    if not token:
        return False
    try:
        n = sb.execute_script(_INJECT_TOKEN_JS, token)
    except Exception as e:
        log.debug("inject_turnstile_token standard JS failed: %s", e)
        try:
            cdp_js = _INJECT_TOKEN_CDP_TEMPLATE.replace("__TOKEN_JSON__", json.dumps(token))
            n = sb.execute_script(cdp_js)
        except Exception as e2:
            log.warning("inject_turnstile_token failed: %s", e2)
            return False
    n = int(n or 0)
    log.info("Injected Turnstile token into %d field(s)/callback(s).", n)
    return n > 0


def solve_cloudflare_clearance(sb, cfg, website_url: str) -> bool:
    """Solve a Cloudflare managed challenge and inject returned cookies."""
    if not cfg.captcha_enabled:
        return False
    proxy = (getattr(cfg, "captcha_proxy", "") or getattr(cfg, "proxy", "") or "").strip()
    if not proxy:
        log.warning("Cloudflare challenge solver needs network.proxy, but proxy is empty.")
        return False
    try:
        solver = get_solver(cfg)
    except CaptchaError as e:
        log.warning("Captcha solver disabled: %s", e)
        return False
    if solver is None:
        return False

    try:
        user_agent = sb.execute_script("return navigator.userAgent") or None
    except Exception:
        user_agent = None

    retries = int((getattr(cfg, "raw", {}) or {}).get("captcha", {}).get("cloudflare_retries", 2))
    retries = max(1, retries)
    solution: dict[str, Any] | None = None
    challenge_html: str | None = None
    last_error: CaptchaError | None = None
    for attempt in range(1, retries + 1):
        if attempt > 1 and challenge_html is None:
            challenge_html = _fetch_cloudflare_challenge_html(website_url, proxy, user_agent)

        log.info(
            "Asking %s to solve Cloudflare challenge for %s (attempt %d/%d%s)",
            cfg.captcha_provider,
            website_url,
            attempt,
            retries,
            ", with html" if challenge_html else "",
        )
        try:
            solution = solver.solve_cloudflare_challenge(
                website_url,
                proxy,
                user_agent=user_agent,
                html=challenge_html,
            )
            break
        except CaptchaError as e:
            last_error = e
            log.warning("Cloudflare challenge solve attempt failed: %s", e)
            continue

    if not solution:
        log.error("Cloudflare challenge solver failed: %s", last_error)
        return False

    returned_ua = solution.get("userAgent")
    if user_agent and returned_ua and returned_ua != user_agent:
        log.warning("Cloudflare solver returned a different User-Agent; clearance may be rejected.")

    cookies = _solution_cookies(solution)
    token = solution.get("token")
    if token and "cf_clearance" not in cookies:
        cookies = {**cookies, "cf_clearance": token}
    if not cookies:
        log.warning("Cloudflare solver returned no cookies.")
        return False

    ok = True
    for name, value in cookies.items():
        if not value:
            continue
        ok = _set_browser_cookie(sb, website_url, str(name), str(value)) and ok
    if ok:
        log.info("Injected Cloudflare clearance cookie(s) for %s.", _origin(website_url))
    return ok


def _fetch_cloudflare_challenge_html(
    url: str,
    proxy: str,
    user_agent: str | None = None,
) -> str | None:
    """Fetch fresh 403 challenge HTML through the same proxy for CapSolver."""
    proxy_url = _proxy_url_for_requests(proxy)
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "content-type": "application/json",
        "origin": "https://visa.vfsglobal.com",
        "referer": "https://visa.vfsglobal.com/",
    }
    if user_agent:
        headers["user-agent"] = user_agent
    try:
        response = requests.post(
            url,
            json={},
            headers=headers,
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=30,
        )
    except Exception as e:
        log.warning("Could not fetch Cloudflare challenge HTML through proxy: %s", e)
        return None
    text = response.text or ""
    if response.status_code == 403 and ("Just a moment" in text or "_cf_chl_opt" in text):
        log.info("Fetched fresh Cloudflare challenge HTML for solver.")
        return text
    log.debug(
        "Challenge HTML fetch returned status=%s len=%s, not a usable Cloudflare page.",
        response.status_code,
        len(text),
    )
    return None


def _proxy_url_for_requests(proxy: str) -> str:
    proxy = (proxy or "").strip()
    if "://" in proxy:
        return proxy
    return f"http://{proxy}"


def _solution_cookies(solution: dict[str, Any]) -> dict[str, str]:
    raw = solution.get("cookies")
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items() if v}
    if isinstance(raw, list):
        out: dict[str, str] = {}
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            value = item.get("value")
            if name and value:
                out[str(name)] = str(value)
        return out
    return {}


def _set_browser_cookie(sb, url: str, name: str, value: str) -> bool:
    driver = getattr(sb, "driver", None)
    if not driver or not hasattr(driver, "execute_cdp_cmd"):
        log.warning("Cannot set cross-domain Cloudflare cookie: browser driver has no CDP API.")
        return False
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        result = driver.execute_cdp_cmd(
            "Network.setCookie",
            {
                "url": _origin(url),
                "name": name,
                "value": value,
                "path": "/",
                "secure": True,
                "httpOnly": True,
            },
        )
    except Exception as e:
        log.warning("Failed to inject Cloudflare cookie %s: %s", name, e)
        return False
    if result and result.get("success") is False:
        log.warning("Chrome rejected Cloudflare cookie %s: %s", name, result)
        return False
    return True


def _origin(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url
    return f"{parsed.scheme}://{parsed.netloc}"


def page_url(sb) -> str:
    try:
        return sb.get_current_url() or ""
    except Exception:
        return ""

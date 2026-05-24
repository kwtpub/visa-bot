r"""Minimal smoke-test for bot.captcha CapSolver flow.

Uses unittest.mock to replace requests.post — does NOT hit the network and
does NOT cost any traffic / money.

Run with:
    .\.venv\Scripts\python.exe -m tests.test_captcha
"""
from __future__ import annotations

import sys
import types
import importlib.util
from unittest import mock

if importlib.util.find_spec("requests") is None:
    sys.modules["requests"] = types.SimpleNamespace(post=None)

if importlib.util.find_spec("yaml") is None:
    sys.modules["yaml"] = types.SimpleNamespace(YAMLError=Exception, safe_load=lambda _: {})

from bot.captcha import CapSolver, CaptchaError, get_solver
from bot.config import Config


# --- fake config object ----------------------------------------------------
def fake_cfg(provider="capsolver", api_key="CAP-TEST", timeout=10):
    c = types.SimpleNamespace()
    c.captcha_provider = provider
    c.captcha_api_key = api_key
    c.captcha_timeout = timeout
    return c


# --- helpers ---------------------------------------------------------------
def _resp(payload):
    r = mock.Mock()
    r.json.return_value = payload
    r.text = str(payload)
    return r


# --- tests -----------------------------------------------------------------
def test_get_solver_disabled_when_no_key():
    s = get_solver(fake_cfg(api_key=""))
    assert s is None, "solver must be None when api_key empty"
    s = get_solver(fake_cfg(provider="none", api_key="CAP-X"))
    assert s is None, "solver must be None when provider=none"


def test_get_solver_returns_capsolver():
    s = get_solver(fake_cfg())
    assert isinstance(s, CapSolver), f"expected CapSolver, got {type(s)}"


def test_solve_turnstile_happy_path():
    cs = CapSolver("CAP-X", timeout_seconds=5)
    create = _resp({"errorId": 0, "taskId": "abc-123"})
    poll1 = _resp({"errorId": 0, "status": "processing"})
    poll2 = _resp({"errorId": 0, "status": "ready",
                   "solution": {"token": "0.AAAA-fake-token"}})
    with mock.patch("bot.captcha.requests.post",
                    side_effect=[create, poll1, poll2]) as m, \
         mock.patch("bot.captcha.time.sleep", return_value=None):
        token = cs.solve_turnstile("0xSITEKEY", "https://example.com/login")
    assert token == "0.AAAA-fake-token", token
    # verify endpoints
    urls = [call.args[0] for call in m.call_args_list]
    assert urls[0].endswith("/createTask"), urls
    assert urls[1].endswith("/getTaskResult"), urls
    assert urls[2].endswith("/getTaskResult"), urls


def test_solve_turnstile_create_error():
    cs = CapSolver("CAP-X", timeout_seconds=5)
    bad = _resp({"errorId": 1, "errorCode": "ERROR_KEY_INVALID",
                 "errorDescription": "Bad key"})
    with mock.patch("bot.captcha.requests.post", return_value=bad):
        try:
            cs.solve_turnstile("0xSITEKEY", "https://example.com/login")
        except CaptchaError as e:
            assert "ERROR_KEY_INVALID" in str(e), str(e)
            return
    raise AssertionError("expected CaptchaError")


def test_solve_turnstile_timeout():
    cs = CapSolver("CAP-X", timeout_seconds=1)  # tiny timeout
    create = _resp({"errorId": 0, "taskId": "t-1"})
    proc = _resp({"errorId": 0, "status": "processing"})
    with mock.patch("bot.captcha.requests.post",
                    side_effect=[create] + [proc] * 50), \
         mock.patch("bot.captcha.time.sleep", return_value=None), \
         mock.patch("bot.captcha.time.time", side_effect=[100.0, 100.0, 121.0]):
        try:
            cs.solve_turnstile("0xSITEKEY", "https://example.com/login")
        except CaptchaError as e:
            assert "timed out" in str(e).lower(), str(e)
            return
    raise AssertionError("expected CaptchaError on timeout")


def test_solve_cloudflare_challenge_happy_path():
    cs = CapSolver("CAP-X", timeout_seconds=5)
    create = _resp({"errorId": 0, "taskId": "cf-123"})
    ready = _resp({
        "errorId": 0,
        "status": "ready",
        "solution": {
            "cookies": {"cf_clearance": "CLEARANCE"},
            "token": "CLEARANCE",
            "userAgent": "UA",
        },
    })
    with mock.patch("bot.captcha.requests.post", side_effect=[create, ready]) as m, \
         mock.patch("bot.captcha.time.sleep", return_value=None):
        sol = cs.solve_cloudflare_challenge(
            "https://lift-api.vfsglobal.com/appointment/CheckIsSlotAvailable",
            "user:pass@proxy.example:1000",
            user_agent="UA",
        )
    assert sol["cookies"]["cf_clearance"] == "CLEARANCE"
    task = m.call_args_list[0].kwargs["json"]["task"]
    assert task["type"] == "AntiCloudflareTask"
    assert task["proxy"] == "http://user:pass@proxy.example:1000"
    assert task["userAgent"] == "UA"


def test_solve_cloudflare_challenge_can_send_html():
    cs = CapSolver("CAP-X", timeout_seconds=5)
    create = _resp({"errorId": 0, "taskId": "cf-html"})
    ready = _resp({
        "errorId": 0,
        "status": "ready",
        "solution": {"token": "CLEARANCE"},
    })
    with mock.patch("bot.captcha.requests.post", side_effect=[create, ready]) as m, \
         mock.patch("bot.captcha.time.sleep", return_value=None):
        sol = cs.solve_cloudflare_challenge(
            "https://lift-api.vfsglobal.com/appointment/CheckIsSlotAvailable",
            "http://user:pass@proxy.example:1000",
            user_agent="UA",
            html="<!DOCTYPE html><title>Just a moment...</title>",
        )
    assert sol["token"] == "CLEARANCE"
    task = m.call_args_list[0].kwargs["json"]["task"]
    assert task["html"].startswith("<!DOCTYPE html>")


def test_solver_factory_unknown_provider():
    try:
        get_solver(fake_cfg(provider="nopecaptcha"))
    except CaptchaError as e:
        assert "Unknown" in str(e), str(e)
        return
    raise AssertionError("expected CaptchaError on unknown provider")


def test_config_captcha_key_can_come_from_default_env():
    cfg = Config(raw={"captcha": {"api_key": ""}})
    with mock.patch.dict("os.environ", {"CAPSOLVER_API_KEY": "CAP-ENV"}):
        assert cfg.captcha_api_key == "CAP-ENV"


def test_config_captcha_key_can_reference_named_env():
    cfg = Config(raw={"captcha": {"api_key": "env:MY_CAPSOLVER_TOKEN"}})
    with mock.patch.dict("os.environ", {"MY_CAPSOLVER_TOKEN": "CAP-NAMED"}):
        assert cfg.captcha_api_key == "CAP-NAMED"


def test_config_proxy_can_reference_named_env():
    cfg = Config(raw={"network": {"proxy": "env:VFS_PROXY_TEST"}})
    with mock.patch.dict("os.environ", {"VFS_PROXY_TEST": "user:pass@host:9000"}):
        assert cfg.proxy == "user:pass@host:9000"


def test_config_captcha_proxy_defaults_to_network_proxy():
    cfg = Config(raw={"network": {"proxy": "user:pass@host:9000"}, "captcha": {}})
    assert cfg.captcha_proxy == "user:pass@host:9000"


def test_config_captcha_proxy_can_reference_env():
    cfg = Config(raw={
        "network": {"proxy": "user:pass@host:9000"},
        "captcha": {"proxy": "env:VFS_CAPTCHA_PROXY_TEST"},
    })
    with mock.patch.dict("os.environ", {"VFS_CAPTCHA_PROXY_TEST": "user2:pass2@host2:9001"}):
        assert cfg.captcha_proxy == "user2:pass2@host2:9001"


def test_config_notletters_key_can_reference_env():
    cfg = Config(raw={"otp": {"notletters": {"api_key": "env:NOTLETTERS_TOKEN"}}})
    with mock.patch.dict("os.environ", {"NOTLETTERS_TOKEN": "NL-KEY"}):
        assert cfg.notletters_cfg["api_key"] == "NL-KEY"


# --- runner ----------------------------------------------------------------
def main():
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  OK  {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {type(e).__name__}: {e}")
    print()
    print(f"Ran {len(tests)} tests, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

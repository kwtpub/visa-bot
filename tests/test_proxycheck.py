from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import requests

from bot.proxycheck import ProxyDead, precheck_proxy


def cfg(**overrides):
    defaults = {
        "proxy": "user:pass@proxy.example:1000",
        "proxy_precheck_enabled": True,
        "proxy_check_url": "https://api.ipify.org?format=json",
        "proxy_check_timeout": 20,
        "debugger_address": "",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def response(status=200, payload=None, text=""):
    r = mock.Mock()
    r.status_code = status
    r.text = text
    if payload is None:
        r.json.side_effect = ValueError("not json")
    else:
        r.json.return_value = payload
    return r


def test_precheck_proxy_skips_when_no_proxy():
    with mock.patch("bot.proxycheck.requests.get") as get:
        precheck_proxy(cfg(proxy=""))
    get.assert_not_called()


def test_precheck_proxy_skips_attached_browser():
    with mock.patch("bot.proxycheck.requests.get") as get:
        precheck_proxy(cfg(debugger_address="127.0.0.1:9222"))
    get.assert_not_called()


def test_precheck_proxy_success_adds_http_scheme():
    with mock.patch("bot.proxycheck.requests.get", return_value=response(payload={"ip": "198.16.1.2"})) as get:
        precheck_proxy(cfg())
    assert get.call_args.kwargs["proxies"] == {
        "http": "http://user:pass@proxy.example:1000",
        "https": "http://user:pass@proxy.example:1000",
    }
    assert get.call_args.kwargs["timeout"] == 20


def test_precheck_proxy_raises_on_http_error():
    with mock.patch("bot.proxycheck.requests.get", return_value=response(status=407, text="auth required")):
        try:
            precheck_proxy(cfg())
        except ProxyDead as e:
            assert "PROXY_DEAD" in str(e)
            assert "407" in str(e)
            return
    raise AssertionError("expected ProxyDead")


def test_precheck_proxy_raises_on_request_exception():
    with mock.patch("bot.proxycheck.requests.get", side_effect=requests.exceptions.ProxyError("cannot connect")):
        try:
            precheck_proxy(cfg())
        except ProxyDead as e:
            assert "PROXY_DEAD" in str(e)
            return
    raise AssertionError("expected ProxyDead")

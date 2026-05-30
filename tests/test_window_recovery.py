"""Tests for browser-window recovery after a stalled proxy closes the target.

A SOCKS5 proxy that stalls and resolves to ERR_TIMED_OUT can leave Chrome's
active target closed; every subsequent navigation then throws "no such window".
`_open_login_page` must recognise that (`_window_is_dead`) and reopen a live
window (`_recover_dead_window`) before retrying, instead of burning all retries
against a dead target.
"""
from __future__ import annotations

from unittest import mock

import pytest

from bot import login


# --- _window_is_dead -------------------------------------------------------

@pytest.mark.parametrize(
    "msg",
    [
        "no such window: target window already closed",
        "web view not found",
        "target window already closed\nfrom unknown error: web view not found",
        "target closed",
        "no such execution context",
        "NO SUCH WINDOW",  # case-insensitive
    ],
)
def test_window_is_dead_true_for_target_closed_errors(msg):
    assert login._window_is_dead(Exception(msg)) is True


@pytest.mark.parametrize(
    "msg",
    [
        "timed out",
        "net::ERR_TIMED_OUT",
        "element not found",
        "connection refused",
        "",
    ],
)
def test_window_is_dead_false_for_other_errors(msg):
    assert login._window_is_dead(Exception(msg)) is False


# --- _recover_dead_window --------------------------------------------------

def _fake_sb_with_driver(driver):
    sb = mock.Mock()
    sb.driver = driver
    return sb


def test_recover_switches_to_surviving_handle():
    driver = mock.Mock()
    driver.window_handles = ["h1", "h2"]
    driver.current_url = "about:blank"

    assert login._recover_dead_window(_fake_sb_with_driver(driver)) is True
    # Switched to the most recent surviving handle; never needed to open one.
    driver.switch_to.window.assert_called_once_with("h2")
    driver.switch_to.new_window.assert_not_called()


def test_recover_opens_new_window_when_no_handles():
    driver = mock.Mock()
    driver.current_url = "about:blank"
    # First access (switch-to-surviving) sees no handles; opening a tab adds one.
    driver.window_handles = []

    def open_tab(kind):
        driver.window_handles = ["fresh"]
    driver.switch_to.new_window.side_effect = open_tab

    assert login._recover_dead_window(_fake_sb_with_driver(driver)) is True
    driver.switch_to.new_window.assert_called_once_with("tab")
    driver.switch_to.window.assert_called_with("fresh")


def test_recover_falls_back_to_window_open_script():
    driver = mock.Mock()
    driver.current_url = "about:blank"
    driver.window_handles = []
    # Both new_window variants fail; the execute_script opener succeeds.
    driver.switch_to.new_window.side_effect = Exception("new_window unsupported")

    def js_open(script):
        driver.window_handles = ["js"]
    driver.execute_script.side_effect = js_open

    assert login._recover_dead_window(_fake_sb_with_driver(driver)) is True
    driver.execute_script.assert_called()  # JS fallback was used


def test_recover_returns_false_when_nothing_works():
    driver = mock.Mock()
    driver.window_handles = []
    driver.switch_to.new_window.side_effect = Exception("nope")
    driver.execute_script.side_effect = Exception("nope")

    assert login._recover_dead_window(_fake_sb_with_driver(driver)) is False


def test_recover_returns_false_without_driver():
    sb = mock.Mock()
    sb.driver = None
    assert login._recover_dead_window(sb) is False


# --- wiring: _open_login_page recovers a dead window then retries -----------

def test_open_login_page_recovers_dead_window_before_retry():
    sb = mock.Mock()
    cfg = mock.Mock()
    cfg.login_url = "https://visa.vfsglobal.com/rus/en/fra/login"

    # Attempt 1: uc_open raises a dead-window error; attempt 2: succeeds.
    sb.uc_open_with_reconnect.side_effect = [
        Exception("no such window: target window already closed"),
        None,
    ]

    with (
        mock.patch.object(login, "_recover_dead_window", return_value=True) as recover,
        mock.patch.object(login, "_navigate_without_wait", return_value=True),
        mock.patch.object(login, "_page_looks_blank_or_error", return_value=False),
        mock.patch.object(login, "human_pause"),
        mock.patch.object(login, "_stop_page_loading"),
    ):
        # Should NOT raise — it recovers the window, the fallback nav succeeds,
        # the page isn't blank/error, so the first attempt completes.
        login._open_login_page(sb, cfg)

    recover.assert_called_once()  # recovery ran exactly once for the dead window


def test_open_login_page_does_not_recover_on_plain_timeout():
    sb = mock.Mock()
    cfg = mock.Mock()
    cfg.login_url = "https://visa.vfsglobal.com/rus/en/fra/login"
    # A non-window error (plain timeout) on attempt 1, then success.
    sb.uc_open_with_reconnect.side_effect = [Exception("timed out"), None]

    with (
        mock.patch.object(login, "_recover_dead_window", return_value=True) as recover,
        mock.patch.object(login, "_navigate_without_wait", return_value=True),
        mock.patch.object(login, "_page_looks_blank_or_error", return_value=False),
        mock.patch.object(login, "human_pause"),
        mock.patch.object(login, "_stop_page_loading"),
    ):
        login._open_login_page(sb, cfg)

    recover.assert_not_called()  # plain timeout is not a dead window

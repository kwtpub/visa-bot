"""Tests for the post-navigation 'is the login page usable?' guard.

Regression cover for the bug where a preloaded-Turnstile navigation through a
stalled SOCKS5 proxy was treated as a successful load: Chrome only paints
ERR_TIMED_OUT ~30-60s later, so a 3-5s check saw a still-"loading" page, skipped
the retrying open path, and then failed waiting 15s for a login form that never
came. `_wait_for_login_page_usable` must instead wait until the page is truly
usable (form/Turnstile) OR clearly errored, and report not-usable on a stall so
the caller falls back to the retrying open path.
"""
from __future__ import annotations

from unittest import mock

from bot import login


def test_returns_true_when_form_present_immediately():
    with (
        mock.patch.object(login, "_page_looks_blank_or_error", return_value=False),
        mock.patch.object(login, "_login_form_or_turnstile_present", return_value=True),
        mock.patch.object(login.time, "sleep") as sleep,
    ):
        assert login._wait_for_login_page_usable(mock.Mock(), timeout=25.0) is True
    sleep.assert_not_called()  # usable on first poll, no waiting


def test_returns_false_immediately_on_chrome_error():
    # ERR_TIMED_OUT / chrome-error:// surfaces -> not usable, caller should retry.
    with (
        mock.patch.object(login, "_page_looks_blank_or_error", return_value=True),
        mock.patch.object(login, "_login_form_or_turnstile_present", return_value=False),
        mock.patch.object(login.time, "sleep") as sleep,
    ):
        assert login._wait_for_login_page_usable(mock.Mock(), timeout=25.0) is False
    sleep.assert_not_called()


def test_returns_false_on_stall_timeout():
    # The exact run #4 failure mode: page never errors AND never renders a form
    # within the window (proxy stalled, ERR_TIMED_OUT not painted yet). Must
    # report not-usable so the caller falls back to the retrying open path.
    times = iter([1000.0, 1000.0, 1001.0, 1100.0])  # start, then past the deadline

    with (
        mock.patch.object(login, "_page_looks_blank_or_error", return_value=False),
        mock.patch.object(login, "_login_form_or_turnstile_present", return_value=False),
        mock.patch.object(login.time, "time", side_effect=lambda: next(times)),
        mock.patch.object(login.time, "sleep"),
    ):
        assert login._wait_for_login_page_usable(mock.Mock(), timeout=25.0) is False


def test_returns_true_when_form_appears_after_a_poll():
    # Page is briefly loading (no form, no error) then the form renders.
    present = iter([False, True])
    times = iter([1000.0, 1000.0, 1000.5, 1001.0])

    with (
        mock.patch.object(login, "_page_looks_blank_or_error", return_value=False),
        mock.patch.object(login, "_login_form_or_turnstile_present", side_effect=lambda sb: next(present)),
        mock.patch.object(login.time, "time", side_effect=lambda: next(times)),
        mock.patch.object(login.time, "sleep") as sleep,
    ):
        assert login._wait_for_login_page_usable(mock.Mock(), timeout=25.0) is True
    sleep.assert_called_once()  # waited exactly one poll before the form appeared


def test_form_present_helper_short_circuits_before_turnstile():
    sb = mock.Mock()
    with (
        mock.patch.object(login, "first_visible", return_value="input#email") as fv,
        mock.patch.object(login, "_turnstile_present") as tp,
    ):
        assert login._login_form_or_turnstile_present(sb) is True
    fv.assert_called_once()
    tp.assert_not_called()  # form found -> don't bother checking Turnstile


def test_form_helper_falls_back_to_turnstile_when_no_form():
    sb = mock.Mock()
    with (
        mock.patch.object(login, "first_visible", return_value=None),
        mock.patch.object(login, "_turnstile_present", return_value=True) as tp,
    ):
        assert login._login_form_or_turnstile_present(sb) is True
    tp.assert_called_once()

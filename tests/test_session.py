from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bot.config import Config
from bot.login import LoginError, _page_looks_blank_or_error, _pass_turnstile, auto_login, looks_logged_in
from bot.main import _parse_args
from bot.session import _normalise_cookie, portal_origin


def test_portal_origin_from_login_url():
    assert portal_origin("https://visa.vfsglobal.com/rus/ru/svn/login") == "https://visa.vfsglobal.com/"


def test_normalise_host_cookie_removes_domain():
    cookie = {
        "name": "__Host-test",
        "value": "abc",
        "domain": ".vfsglobal.com",
        "path": "/bad",
        "secure": False,
    }
    out = _normalise_cookie(cookie)
    assert out["name"] == "__Host-test"
    assert out["path"] == "/"
    assert out["secure"] is True
    assert "domain" not in out


def test_config_session_state_file_can_reference_env():
    cfg = Config(raw={"session": {"cookies_file": "env:VFS_SESSION_FILE"}})
    with mock.patch.dict("os.environ", {"VFS_SESSION_FILE": "saved_cookies/test.json"}):
        path = cfg.session_state_file
    assert path is not None
    assert path.name == "test.json"
    assert path.parent.name == "saved_cookies"


def test_config_session_state_file_resolves_relative_path():
    cfg = Config(raw={"session": {"cookies_file": "saved_cookies/test.json"}})
    path = cfg.session_state_file
    assert isinstance(path, Path)
    assert path.is_absolute()
    assert path.name == "test.json"


def test_config_manual_login_defaults_and_override():
    cfg = Config(raw={})
    assert cfg.manual_login_enabled is False
    assert cfg.manual_login_wait_seconds == 300
    assert cfg.page_load_timeout == 35

    cfg = Config(raw={"session": {"manual_login": True, "manual_login_wait_seconds": 120}})
    assert cfg.manual_login_enabled is True
    assert cfg.manual_login_wait_seconds == 120


def test_auto_login_temporarily_disables_manual_login():
    cfg = Config(raw={"session": {"manual_login": True}})
    sb = mock.Mock()

    with mock.patch("bot.login.perform_login") as perform:
        auto_login(sb, cfg)

    perform.assert_called_once_with(sb, cfg)
    assert cfg.manual_login_enabled is True


def test_parse_args_login_mode_flags():
    assert _parse_args(["--auto-login"]).auto_login is True
    assert _parse_args(["--manual-login"]).manual_login is True


def test_page_looks_blank_when_vfs_angular_shell_is_empty():
    class Driver:
        def execute_cdp_cmd(self, method, params):
            assert method == "Runtime.evaluate"
            return {
                "result": {
                    "result": {
                        "value": {
                            "readyState": "loading",
                            "bodyTextLen": 0,
                            "appRoot": True,
                            "appTextLen": 0,
                            "hasLogin": False,
                            "hasTurnstile": False,
                        }
                    }
                }
            }

    sb = mock.Mock()
    sb.driver = Driver()
    sb.get_current_url.return_value = "https://visa.vfsglobal.com/rus/ru/svn/login"
    sb.get_page_source.return_value = "<html><body><app-root></app-root></body></html>"

    assert _page_looks_blank_or_error(sb) is True


def test_pass_turnstile_uses_solver_without_gui_click():
    cfg = SimpleNamespace(captcha_enabled=True, captcha_provider="capsolver")
    sb = mock.Mock()
    sb.uc_gui_click_captcha = mock.Mock(side_effect=AssertionError("must not move mouse"))

    with (
        mock.patch("bot.login.install_turnstile_hook"),
        mock.patch("bot.login._turnstile_present", return_value=True),
        mock.patch("bot.login._wait_for_turnstile_auto_clear", side_effect=[False, False]),
        mock.patch("bot.login._solve_with_paid_service", return_value=True) as solve,
    ):
        _pass_turnstile(sb, cfg)

    solve.assert_called_once_with(sb, cfg)
    sb.uc_gui_click_captcha.assert_not_called()


def test_pass_turnstile_fails_without_solver_instead_of_manual_click():
    cfg = SimpleNamespace(
        captcha_enabled=False,
        captcha_provider="none",
        screenshot_dir=Path("."),
        screenshots_enabled=False,
    )
    sb = mock.Mock()
    sb.uc_gui_click_captcha = mock.Mock(side_effect=AssertionError("must not move mouse"))

    with (
        mock.patch("bot.login.install_turnstile_hook"),
        mock.patch("bot.login._turnstile_present", return_value=True),
        mock.patch("bot.login._wait_for_turnstile_auto_clear", return_value=False),
        mock.patch("bot.login.screenshot"),
    ):
        try:
            _pass_turnstile(sb, cfg)
        except LoginError as e:
            assert "no captcha solver is configured" in str(e)
        else:
            raise AssertionError("expected LoginError")

    sb.uc_gui_click_captcha.assert_not_called()


class _FakeSb:
    def __init__(self, url: str, title: str = ""):
        self.url = url
        self.title = title
        self.driver = mock.Mock(title=title)

    def get_current_url(self):
        return self.url

    def get_title(self):
        return self.title


def test_looks_logged_in_accepts_vfs_dashboard_url():
    sb = _FakeSb("https://visa.vfsglobal.com/rus/ru/svn/dashboard")
    assert looks_logged_in(sb) is True


def test_looks_logged_in_accepts_russian_dashboard_title():
    sb = _FakeSb(
        "https://visa.vfsglobal.com/rus/ru/svn/some-page",
        "\u041f\u0430\u043d\u0435\u043b\u044c \u0438\u043d\u0441\u0442\u0440\u0443\u043c\u0435\u043d\u0442\u043e\u0432 | VFS Global",
    )
    assert looks_logged_in(sb) is True

from __future__ import annotations

from pathlib import Path
from unittest import mock

from bot.config import Config
from bot.login import looks_logged_in
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

    cfg = Config(raw={"session": {"manual_login": True, "manual_login_wait_seconds": 120}})
    assert cfg.manual_login_enabled is True
    assert cfg.manual_login_wait_seconds == 120


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

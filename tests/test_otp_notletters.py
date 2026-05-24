from __future__ import annotations

import time
from unittest import mock

from bot.config import Config
from bot.otp import (
    _extract_code_from_letters,
    _extract_link_from_letters,
    _notletters_fetch_letters,
    get_otp,
)


def _resp(payload):
    response = mock.Mock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def test_notletters_fetch_letters_sends_auth_and_mailbox_credentials():
    payload = {
        "data": {
            "letters": [
                {
                    "sender": "noreply@vfsglobal.com",
                    "subject": "OTP",
                    "letter": {"text": "123456"},
                    "date": int(time.time()),
                }
            ]
        }
    }
    with mock.patch("bot.otp.requests.post", return_value=_resp(payload)) as post:
        letters = _notletters_fetch_letters(
            "https://api.notletters.com",
            "API-KEY",
            "mail@example.com",
            "mail-pass",
            search="VFS",
        )

    assert letters[0]["letter"]["text"] == "123456"
    call = post.call_args
    assert call.args[0] == "https://api.notletters.com/v1/letters"
    assert call.kwargs["headers"]["Authorization"] == "Bearer API-KEY"
    assert call.kwargs["json"]["email"] == "mail@example.com"
    assert call.kwargs["json"]["password"] == "mail-pass"
    assert call.kwargs["json"]["filters"]["search"] == "VFS"


def test_extract_code_from_notletters_letters_prefers_recent_vfs_mail():
    now = int(time.time())
    letters = [
        {
            "sender": "noreply@vfsglobal.com",
            "subject": "old 111111",
            "letter": {"text": ""},
            "date": now - 500,
        },
        {
            "sender": "noreply@vfsglobal.com",
            "subject": "Your verification code",
            "letter": {"text": "Use 654321 to continue"},
            "date": now,
        },
    ]

    assert _extract_code_from_letters(letters, min_date=now - 90, from_contains="vfsglobal") == "654321"


def test_extract_link_from_notletters_letters_finds_activation_link():
    letters = [
        {
            "sender": "noreply@vfsglobal.com",
            "subject": "Activate your account",
            "letter": {
                "html": '<a href="https://visa.vfsglobal.com/rus/ru/svn/activateemail?q=abc&amp;x=1">activate</a>',
                "text": "",
            },
            "date": 200,
        }
    ]

    link = _extract_link_from_letters(
        letters,
        min_date=100,
        from_contains="vfsglobal",
        href_contains="activateemail",
    )

    assert link == "https://visa.vfsglobal.com/rus/ru/svn/activateemail?q=abc&x=1"


def test_get_otp_notletters_does_not_fallback_to_console():
    cfg = Config(raw={
        "otp": {
            "mode": "notletters",
            "notletters": {
                "api_key": "API-KEY",
                "email": "mail@example.com",
                "password": "mail-pass",
            },
        }
    })
    with (
        mock.patch("bot.otp._get_otp_via_notletters", return_value="654321") as reader,
        mock.patch("bot.otp._ask_console", side_effect=AssertionError("must not ask console")),
    ):
        assert get_otp(cfg) == "654321"

    reader.assert_called_once()

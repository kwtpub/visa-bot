from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.accounts import PoolAccount
from bot.config import Config
from bot.registration import (
    RegistrationError,
    _activation_redirected_to_login,
    _looks_activation_completed,
    _registration_submit_accepted,
    build_registration_profile,
    generate_phone_number,
)
from bot.util import xpath_literal


def test_generate_phone_number_uses_configured_prefix_and_length():
    phone = generate_phone_number({"digits": 10, "prefixes": ["999"]})

    assert phone.startswith("999")
    assert len(phone) == 10
    assert phone.isdigit()


def test_build_registration_profile_uses_pool_account_and_applicant_fallbacks():
    cfg = Config(raw={
        "account": {"email": "fixed@example.com", "password": "fixed-pass"},
        "registration": {
            "random_phone": {"country_code": "+7", "digits": 10, "prefixes": ["900"]},
        },
        "applicants": [
            {
                "first_name": "IVAN",
                "last_name": "IVANOV",
                "date_of_birth": "1990-01-01",
                "passport_number": "AB123456",
                "nationality": "Russian",
            }
        ],
    })
    account = PoolAccount(
        email="pool@example.com",
        mailbox_password="mail-pass",
        vfs_password="vfs-pass",
    )

    profile = build_registration_profile(cfg, account)

    assert profile.email == "pool@example.com"
    assert profile.password == "vfs-pass"
    assert profile.first_name == "IVAN"
    assert profile.passport_number == "AB123456"
    assert profile.phone_country_code == "+7"
    assert profile.phone_number.startswith("900")


def test_registration_submit_rejects_validation_error_text():
    class FakeSB:
        def get_page_source(self):
            return ""

        def execute_script(self, script):
            if "mat-error" in script:
                return "Обязательное поле нельзя оставлять пустым"
            return False

    with pytest.raises(RegistrationError, match="Registration rejected"):
        _registration_submit_accepted(FakeSB(), SimpleNamespace(), timeout=0.1)


def test_activation_completed_does_not_accept_plain_login_page():
    class FakeSB:
        def get_current_url(self):
            return "https://visa.vfsglobal.com/rus/ru/svn/login"

        def get_page_source(self):
            return "<input formcontrolname='username'>"

    assert _looks_activation_completed(FakeSB()) is False
    assert _activation_redirected_to_login(FakeSB()) is True


def test_activation_completed_accepts_success_text():
    class FakeSB:
        def get_page_source(self):
            return "Your account activated successfully."

    assert _looks_activation_completed(FakeSB()) is True


def test_xpath_literal_handles_quotes():
    literal = xpath_literal('O\'Brien "Visa"')

    assert literal.startswith("concat(")
    assert '"\'"' in literal

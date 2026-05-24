from __future__ import annotations

from bot.accounts import PoolAccount
from bot.config import Config
from bot.registration import build_registration_profile, generate_phone_number


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

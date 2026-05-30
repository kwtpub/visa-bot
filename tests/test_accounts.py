from __future__ import annotations

import json
from pathlib import Path

from bot.accounts import AccountPool, _read_accounts_file
from bot.config import Config


def test_read_accounts_file_parses_notletters_email_password(tmp_path: Path):
    path = tmp_path / "emails.txt"
    path.write_text(
        "first@example.com:mail-pass-1\n"
        "second@example.com:mail-pass-2:custom-vfs-pass\n",
        encoding="utf-8",
    )

    accounts = _read_accounts_file(path, vfs_password="shared-vfs-pass")

    assert [a.email for a in accounts] == ["first@example.com", "second@example.com"]
    assert accounts[0].mailbox_password == "mail-pass-1"
    assert accounts[0].vfs_password == "shared-vfs-pass"
    assert accounts[1].vfs_password == "custom-vfs-pass"


def test_account_pool_applies_active_account_to_config(tmp_path: Path):
    emails = tmp_path / "emails.txt"
    state = tmp_path / "state.json"
    emails.write_text("pool@example.com:mail-pass\n", encoding="utf-8")
    cfg = Config(raw={
        "account": {"email": "old@example.com", "password": "shared-vfs-pass"},
        "otp": {"mode": "manual", "notletters": {"api_key": "KEY"}},
        "session": {},
        "account_pool": {
            "enabled": True,
            "accounts_file": str(emails),
            "state_file": str(state),
            "vfs_password": "shared-vfs-pass",
        },
    })
    accounts = _read_accounts_file(emails, vfs_password="shared-vfs-pass")
    pool = AccountPool(cfg, accounts)

    assert pool.select_next() is not None
    pool.apply_current()

    assert cfg.email == "pool@example.com"
    assert cfg.password == "shared-vfs-pass"
    assert cfg.otp_mode == "notletters"
    assert cfg.notletters_cfg["email"] == "pool@example.com"
    assert cfg.notletters_cfg["password"] == "mail-pass"
    assert "pool_example.com.json" in str(cfg.session_state_file)


def test_account_pool_rotates_and_persists_cooldown(tmp_path: Path):
    emails = tmp_path / "emails.txt"
    state = tmp_path / "state.json"
    emails.write_text("one@example.com:p1\ntwo@example.com:p2\n", encoding="utf-8")
    cfg = Config(raw={
        "account": {"email": "old@example.com", "password": "vfs-pass"},
        "otp": {"mode": "notletters", "notletters": {"api_key": "KEY"}},
        "session": {},
        "account_pool": {
            "enabled": True,
            "accounts_file": str(emails),
            "state_file": str(state),
            "vfs_password": "vfs-pass",
            "max_failures_per_account": 1,
        },
    })
    pool = AccountPool(cfg, _read_accounts_file(emails, vfs_password="vfs-pass"))

    pool.select_next()
    first = pool.current.email
    assert pool.rotate_after_failure(cfg, "restricted", restricted=True) is True

    assert pool.current.email != first
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["accounts"][first]["status"] == "restricted"


def test_account_pool_rotation_prefers_ready_accounts(tmp_path: Path):
    emails = tmp_path / "emails.txt"
    state = tmp_path / "state.json"
    emails.write_text(
        "fresh@example.com:p1\nready@example.com:p2\nother@example.com:p3\n",
        encoding="utf-8",
    )
    cfg = Config(raw={
        "account": {"email": "old@example.com", "password": "vfs-pass"},
        "otp": {"mode": "notletters", "notletters": {"api_key": "KEY"}},
        "session": {},
        "account_pool": {
            "enabled": True,
            "accounts_file": str(emails),
            "state_file": str(state),
            "vfs_password": "vfs-pass",
            "max_failures_per_account": 1,
        },
    })
    pool = AccountPool(cfg, _read_accounts_file(emails, vfs_password="vfs-pass"))
    pool.accounts[1].status = "healthy"
    pool.current = pool.accounts[0]

    assert pool.rotate_after_failure(cfg, "login failed") is True

    assert pool.current.email == "ready@example.com"


def test_account_pool_registration_status_controls_registered_selection(tmp_path: Path):
    emails = tmp_path / "emails.txt"
    state = tmp_path / "state.json"
    emails.write_text("one@example.com:p1\ntwo@example.com:p2\n", encoding="utf-8")
    cfg = Config(raw={
        "account": {"email": "old@example.com", "password": "vfs-pass"},
        "otp": {"mode": "notletters", "notletters": {"api_key": "KEY"}},
        "session": {},
        "account_pool": {
            "enabled": True,
            "accounts_file": str(emails),
            "state_file": str(state),
            "vfs_password": "vfs-pass",
        },
    })
    pool = AccountPool(cfg, _read_accounts_file(emails, vfs_password="vfs-pass"))

    assert pool.select_next(registered_only=True) is None
    assert pool.select_for_registration() is not None
    first = pool.current.email

    pool.mark_registered()

    assert pool.select_next(registered_only=True).email == first
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["accounts"][first]["status"] == "registered"


def test_account_pool_prefers_fresh_before_needs_activation(tmp_path: Path):
    emails = tmp_path / "emails.txt"
    state = tmp_path / "state.json"
    emails.write_text("one@example.com:p1\ntwo@example.com:p2\n", encoding="utf-8")
    cfg = Config(raw={
        "account": {"email": "old@example.com", "password": "vfs-pass"},
        "otp": {"mode": "notletters", "notletters": {"api_key": "KEY"}},
        "session": {},
        "account_pool": {
            "enabled": True,
            "accounts_file": str(emails),
            "state_file": str(state),
            "vfs_password": "vfs-pass",
        },
    })
    pool = AccountPool(cfg, _read_accounts_file(emails, vfs_password="vfs-pass"))

    assert pool.select_for_registration() is not None
    first = pool.current.email
    pool.mark_needs_activation("VFS says this email is already registered")

    assert pool.select_next(registered_only=True) is None
    assert pool.select_for_registration().email != first
    pool.mark_registered()
    assert pool.select_for_registration().email == first
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["accounts"][first]["status"] == "needs_activation"


def test_account_pool_mark_banned_excludes_account(tmp_path: Path):
    emails = tmp_path / "emails.txt"
    state = tmp_path / "state.json"
    emails.write_text("one@example.com:p1\ntwo@example.com:p2\n", encoding="utf-8")
    cfg = Config(raw={
        "account": {"email": "old@example.com", "password": "vfs-pass"},
        "otp": {"mode": "notletters", "notletters": {"api_key": "KEY"}},
        "session": {},
        "account_pool": {
            "enabled": True,
            "accounts_file": str(emails),
            "state_file": str(state),
            "vfs_password": "vfs-pass",
        },
    })
    pool = AccountPool(cfg, _read_accounts_file(emails, vfs_password="vfs-pass"))

    assert pool.select_for_registration() is not None
    first = pool.current.email
    pool.mark_banned("429201 account blocked")

    assert pool.select_for_registration().email != first
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["accounts"][first]["status"] == "banned"

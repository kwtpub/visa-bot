"""Account pool support for rotating VFS accounts and mailbox credentials."""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .util import log


ROOT = Path(__file__).resolve().parent.parent


@dataclass
class PoolAccount:
    email: str
    mailbox_password: str
    vfs_password: str
    status: str = "fresh"
    fail_count: int = 0
    cooldown_until: float = 0.0
    last_error: str = ""
    last_used: float = 0.0
    cookies_file: str = ""

    @property
    def available(self) -> bool:
        if self.status in {"banned", "bad_credentials"}:
            return False
        return self.cooldown_until <= time.time()

    @property
    def registered(self) -> bool:
        return self.status in {"registered", "healthy"}

    @property
    def needs_activation(self) -> bool:
        return self.status == "needs_activation"


class AccountPool:
    def __init__(self, cfg, accounts: list[PoolAccount]) -> None:
        self.cfg = cfg
        self.settings = cfg.raw.get("account_pool", {}) or {}
        self.accounts = accounts
        self.current: PoolAccount | None = None
        self.state_file = _resolve_path(
            self.settings.get("state_file") or "saved_cookies/account_pool_state.json"
        )
        self.max_failures = int(self.settings.get("max_failures_per_account", 1))
        self.cooldown_minutes = int(self.settings.get("cooldown_minutes", 60))
        self.restricted_cooldown_minutes = int(
            self.settings.get("restricted_cooldown_minutes", 24 * 60)
        )
        self.per_account_cookies = bool(self.settings.get("per_account_cookies", True))
        self._load_state()

    def select_next(
        self,
        *,
        exclude_email: str = "",
        registered_only: bool = False,
    ) -> PoolAccount | None:
        excluded = exclude_email.lower()
        available = [
            acc for acc in self.accounts
            if acc.available and acc.email.lower() != excluded
        ]
        if registered_only:
            available = [acc for acc in available if acc.registered]
        if not available:
            return None
        available.sort(key=lambda acc: (acc.last_used or 0.0, acc.fail_count, acc.email))
        self.current = available[0]
        return self.current

    def select_for_registration(self, *, exclude_email: str = "") -> PoolAccount | None:
        excluded = exclude_email.lower()
        available = [
            acc for acc in self.accounts
            if acc.available
            and not acc.registered
            and acc.email.lower() != excluded
            and acc.status not in {"banned", "bad_credentials"}
        ]
        if not available:
            return None
        available.sort(key=lambda acc: (
            0 if acc.status in {"", "fresh"} else 1 if acc.status == "needs_registration" else 2 if acc.needs_activation else 3,
            acc.last_used or 0.0,
            acc.fail_count,
            acc.email,
        ))
        self.current = available[0]
        return self.current

    def apply_current(self) -> None:
        if not self.current:
            raise RuntimeError("AccountPool.apply_current() called without a current account")
        acc = self.current
        self.cfg.raw.setdefault("account", {})["email"] = acc.email
        self.cfg.raw.setdefault("account", {})["password"] = acc.vfs_password

        otp = self.cfg.raw.setdefault("otp", {})
        if str(otp.get("mode") or "manual").lower() == "manual":
            log.warning("Account pool requires mailbox polling; switching otp.mode from manual to notletters for this account.")
            otp["mode"] = "notletters"
        notletters = otp.setdefault("notletters", {})
        notletters["email"] = acc.email
        notletters["password"] = acc.mailbox_password

        if self.per_account_cookies:
            cookies_file = acc.cookies_file or str(
                Path("saved_cookies") / "accounts" / f"{_safe_filename(acc.email)}.json"
            )
            acc.cookies_file = cookies_file
            self.cfg.raw.setdefault("session", {})["cookies_file"] = cookies_file
            self.cfg.raw.setdefault("session", {})["import_cookies"] = True
            self.cfg.raw.setdefault("session", {})["export_cookies"] = True

        log.info("Using VFS account %s from account pool.", _mask_email(acc.email))

    def mark_success(self) -> None:
        if not self.current:
            return
        acc = self.current
        acc.status = "healthy"
        acc.fail_count = 0
        acc.cooldown_until = 0.0
        acc.last_error = ""
        acc.last_used = time.time()
        self._save_state()

    def mark_registered(self) -> None:
        if not self.current:
            return
        acc = self.current
        acc.status = "registered"
        acc.fail_count = 0
        acc.cooldown_until = 0.0
        acc.last_error = ""
        acc.last_used = time.time()
        self._save_state()

    def mark_registration_failure(self, reason: str) -> None:
        if _looks_activation_needed(reason):
            self.mark_needs_activation(reason)
            return
        if not self.current:
            return
        acc = self.current
        acc.fail_count += 1
        acc.last_error = _short_reason(reason)
        acc.last_used = time.time()
        if acc.fail_count >= self.max_failures:
            acc.status = "cooldown"
            acc.cooldown_until = time.time() + self.cooldown_minutes * 60
        else:
            acc.status = "needs_registration"
        self._save_state()

    def mark_needs_activation(self, reason: str) -> None:
        if not self.current:
            return
        acc = self.current
        acc.status = "needs_activation"
        acc.last_error = _short_reason(reason)
        acc.last_used = time.time()
        acc.cooldown_until = 0.0
        self._save_state()

    def mark_needs_registration(self, reason: str) -> None:
        if not self.current:
            return
        acc = self.current
        acc.status = "needs_registration"
        acc.last_error = _short_reason(reason)
        acc.last_used = time.time()
        acc.cooldown_until = 0.0
        self._save_state()

    def mark_failure(self, reason: str, *, restricted: bool = False) -> None:
        if not self.current:
            return
        acc = self.current
        acc.fail_count += 1
        acc.last_error = _short_reason(reason)
        acc.last_used = time.time()
        if restricted:
            acc.status = "restricted"
            acc.cooldown_until = time.time() + self.restricted_cooldown_minutes * 60
        elif acc.fail_count >= self.max_failures:
            acc.status = "cooldown"
            acc.cooldown_until = time.time() + self.cooldown_minutes * 60
        else:
            acc.status = "retry"
        self._save_state()

    def mark_bad_credentials(self, reason: str) -> None:
        if not self.current:
            return
        acc = self.current
        acc.status = "bad_credentials"
        acc.fail_count += 1
        acc.last_error = _short_reason(reason)
        acc.last_used = time.time()
        self._save_state()

    def mark_banned(self, reason: str) -> None:
        if not self.current:
            return
        acc = self.current
        acc.status = "banned"
        acc.fail_count += 1
        acc.last_error = _short_reason(reason)
        acc.last_used = time.time()
        acc.cooldown_until = 0.0
        self._save_state()

    def rotate_after_failure(
        self,
        cfg,
        reason: str,
        *,
        restricted: bool = False,
        bad_credentials: bool = False,
        registered_only: bool = False,
    ) -> bool:
        if bad_credentials:
            self.mark_bad_credentials(reason)
        else:
            self.mark_failure(reason, restricted=restricted)

        previous = self.current.email if self.current else ""
        nxt = None
        if not registered_only:
            nxt = self.select_next(exclude_email=previous, registered_only=True)
        if not nxt:
            nxt = self.select_next(exclude_email=previous, registered_only=registered_only)
        if not nxt:
            log.error("No available account left in account pool.")
            return False
        self.cfg = cfg
        self.apply_current()
        log.warning(
            "Rotated account %s -> %s after: %s",
            _mask_email(previous),
            _mask_email(nxt.email),
            _short_reason(reason),
        )
        return True

    def _load_state(self) -> None:
        if not self.state_file.exists():
            return
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("Could not read account-pool state from %s: %s", self.state_file, e)
            return
        saved = raw.get("accounts") if isinstance(raw, dict) else {}
        if not isinstance(saved, dict):
            return
        by_email = {acc.email.lower(): acc for acc in self.accounts}
        for email, state in saved.items():
            acc = by_email.get(str(email).lower())
            if not acc or not isinstance(state, dict):
                continue
            acc.status = str(state.get("status") or acc.status)
            acc.fail_count = int(state.get("fail_count") or 0)
            acc.cooldown_until = float(state.get("cooldown_until") or 0.0)
            acc.last_error = str(state.get("last_error") or "")
            if acc.status in {"needs_registration", "cooldown", "retry"} and _looks_activation_needed(acc.last_error):
                acc.status = "needs_activation"
            if acc.status in {"needs_registration", "cooldown", "retry"} and _looks_login_verification_needed(acc.last_error):
                acc.status = "registered"
            acc.last_used = float(state.get("last_used") or 0.0)
            acc.cookies_file = str(state.get("cookies_file") or acc.cookies_file)

    def _save_state(self) -> None:
        data = {
            "accounts": {
                acc.email: {
                    "status": acc.status,
                    "fail_count": acc.fail_count,
                    "cooldown_until": acc.cooldown_until,
                    "last_error": acc.last_error,
                    "last_used": acc.last_used,
                    "cookies_file": acc.cookies_file,
                }
                for acc in self.accounts
            }
        }
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_file)


def load_account_pool(cfg) -> AccountPool | None:
    settings = cfg.raw.get("account_pool", {}) or {}
    if not settings or not bool(settings.get("enabled", False)):
        return None
    accounts_file = _resolve_path(settings.get("accounts_file") or "needed/emails.txt")
    if not accounts_file.exists():
        raise RuntimeError(f"account_pool.accounts_file not found: {accounts_file}")
    vfs_password = str(settings.get("vfs_password") or cfg.raw.get("account", {}).get("password") or "")
    if not vfs_password:
        raise RuntimeError("account_pool.vfs_password or account.password must be set.")

    accounts = _read_accounts_file(accounts_file, vfs_password=vfs_password)
    if not accounts:
        raise RuntimeError(f"No accounts found in {accounts_file}")
    return AccountPool(cfg, accounts)


def _read_accounts_file(path: Path, *, vfs_password: str) -> list[PoolAccount]:
    out: list[PoolAccount] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parsed = _parse_account_line(line, default_vfs_password=vfs_password)
        if not parsed:
            log.warning("Skipping malformed account-pool line in %s.", path)
            continue
        key = parsed.email.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(parsed)
    return out


def _parse_account_line(line: str, *, default_vfs_password: str) -> PoolAccount | None:
    if "@" not in line:
        return None
    for sep in (";", "|", ","):
        if sep in line:
            parts = [p.strip() for p in line.split(sep)]
            break
    else:
        parts = [p.strip() for p in line.split(":", 2)]
    if len(parts) < 2:
        return None
    email = parts[0]
    mailbox_password = parts[1]
    vfs_password = parts[2] if len(parts) >= 3 and parts[2] else default_vfs_password
    if "@" not in email or not mailbox_password:
        return None
    return PoolAccount(email=email, mailbox_password=mailbox_password, vfs_password=vfs_password)


def _resolve_path(value: str | Path) -> Path:
    path = Path(str(value))
    if not path.is_absolute():
        path = ROOT / path
    return path


def _safe_filename(email: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", email).strip("._") or "account"


def _mask_email(email: str) -> str:
    if not email or "@" not in email:
        return "<account>"
    name, domain = email.split("@", 1)
    if len(name) <= 2:
        masked = name[:1] + "*"
    else:
        masked = name[:2] + "***" + name[-1:]
    return f"{masked}@{domain}"


def _short_reason(reason: str, limit: int = 240) -> str:
    reason = " ".join(str(reason).split())
    return reason[:limit]


def _looks_activation_needed(reason: str) -> bool:
    text = str(reason or "").lower()
    return any(
        marker in text
        for marker in (
            "already registered",
            "already exists",
            "email already",
            "activation email",
            "activation link",
            "no activation email",
            "no activation link",
            "vfs says this email is already registered",
            "уже зарегистр",
            "уже существ",
        )
    )


def _looks_login_verification_needed(reason: str) -> bool:
    text = str(reason or "").lower()
    return "login verification failed" in text and not _looks_not_registered(text)


def _looks_not_registered(reason: str) -> bool:
    text = str(reason or "").lower()
    return any(
        marker in text
        for marker in (
            "not registered",
            "not yet registered",
            "не зарегистр",
        )
    )

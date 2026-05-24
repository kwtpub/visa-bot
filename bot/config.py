"""Load and validate config.yaml."""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"
BASE_URL = "https://visa.vfsglobal.com"


@dataclass
class Config:
    raw: dict[str, Any] = field(default_factory=dict)

    # convenience accessors -------------------------------------------------
    @property
    def login_url(self) -> str:
        portal = self.raw["portal"]
        explicit = (portal.get("login_url") or "").strip()
        if explicit:
            return explicit
        seg = portal["url_segment"].strip("/")
        return f"{BASE_URL}/{seg}/login"

    @property
    def email(self) -> str:
        return self.raw["account"]["email"]

    @property
    def password(self) -> str:
        return self.raw["account"]["password"]

    @property
    def auto_book(self) -> bool:
        return bool(self.raw["behaviour"]["auto_book"])

    @property
    def check_interval(self) -> int:
        return int(self.raw["behaviour"]["check_interval_seconds"])

    @property
    def jitter(self) -> tuple[int, int]:
        lo, hi = self.raw["behaviour"]["jitter_seconds"]
        return int(lo), int(hi)

    @property
    def stop_after_bookings(self) -> int:
        return int(self.raw["behaviour"].get("stop_after_bookings", 0))

    @property
    def auto_book_dry_run(self) -> bool:
        return bool(self.raw.get("behaviour", {}).get("auto_book_dry_run", False))

    @property
    def relogin_after_failures(self) -> int:
        return int(self.raw["behaviour"].get("relogin_after_failures", 3))

    @property
    def proxy(self) -> str:
        value = (self.raw.get("network", {}).get("proxy") or "").strip()
        if value.lower().startswith("env:"):
            env_name = value.split(":", 1)[1].strip()
            return os.getenv(env_name, "").strip()
        return value or os.getenv("VFS_PROXY", "").strip()

    @property
    def headless(self) -> bool:
        return bool(self.raw.get("network", {}).get("headless", False))

    @property
    def chrome_version(self) -> str:
        return str(self.raw.get("network", {}).get("chrome_version") or "").strip()

    @property
    def debugger_address(self) -> str:
        return str(
            self.raw.get("network", {}).get("debugger_address")
            or os.getenv("VFS_DEBUGGER_ADDRESS", "")
        ).strip()

    @property
    def remote_debug_port(self) -> int:
        value = str(
            self.raw.get("network", {}).get("remote_debug_port")
            or os.getenv("VFS_REMOTE_DEBUG_PORT", "")
        ).strip()
        if not value:
            return 0
        return int(value)

    @property
    def proxy_precheck_enabled(self) -> bool:
        return bool(self.raw.get("network", {}).get("proxy_precheck", True))

    @property
    def proxy_auth_bridge_enabled(self) -> bool:
        return bool(self.raw.get("network", {}).get("proxy_auth_bridge", True))

    @property
    def proxy_check_url(self) -> str:
        return str(
            self.raw.get("network", {}).get("proxy_check_url")
            or "https://api.ipify.org?format=json"
        ).strip()

    @property
    def proxy_check_timeout(self) -> int:
        return int(self.raw.get("network", {}).get("proxy_check_timeout_seconds", 20))

    @property
    def page_load_timeout(self) -> int:
        return int(self.raw.get("network", {}).get("page_load_timeout_seconds", 35))

    # --- session reuse -----------------------------------------------------
    @property
    def session_state_file(self) -> Path | None:
        value = str(
            self.raw.get("session", {}).get("cookies_file")
            or os.getenv("VFS_SESSION_COOKIES", "")
        ).strip()
        if value.lower().startswith("env:"):
            env_name = value.split(":", 1)[1].strip()
            value = os.getenv(env_name, "").strip()
        if not value:
            return None
        p = Path(value)
        if not p.is_absolute():
            p = Path(__file__).resolve().parent.parent / p
        return p

    @property
    def session_import_enabled(self) -> bool:
        return bool(self.raw.get("session", {}).get("import_cookies", True))

    @property
    def session_export_enabled(self) -> bool:
        return bool(self.raw.get("session", {}).get("export_cookies", True))

    @property
    def manual_login_enabled(self) -> bool:
        return bool(self.raw.get("session", {}).get("manual_login", False))

    @property
    def manual_login_wait_seconds(self) -> int:
        return int(self.raw.get("session", {}).get("manual_login_wait_seconds", 300))

    # --- captcha ----------------------------------------------------------
    @property
    def captcha_provider(self) -> str:
        return str(self.raw.get("captcha", {}).get("provider") or "none").strip().lower()

    @property
    def captcha_api_key(self) -> str:
        value = str(self.raw.get("captcha", {}).get("api_key") or "").strip()
        if value.lower().startswith("env:"):
            env_name = value.split(":", 1)[1].strip()
            return os.getenv(env_name, "").strip()
        return value or os.getenv("CAPSOLVER_API_KEY", "").strip()

    @property
    def captcha_timeout(self) -> int:
        return int(self.raw.get("captcha", {}).get("timeout_seconds", 120))

    @property
    def captcha_proxy(self) -> str:
        value = str(
            self.raw.get("captcha", {}).get("proxy")
            or self.raw.get("captcha", {}).get("cloudflare_proxy")
            or os.getenv("VFS_CAPTCHA_PROXY", "")
        ).strip()
        if value.lower().startswith("env:"):
            env_name = value.split(":", 1)[1].strip()
            value = os.getenv(env_name, "").strip()
        return value or self.proxy

    @property
    def captcha_enabled(self) -> bool:
        return self.captcha_provider not in ("none", "off", "") and bool(self.captcha_api_key)

    @property
    def appointment(self) -> dict[str, Any]:
        return self.raw["appointment"]

    @property
    def applicants_count(self) -> int:
        appt = self.appointment
        value = appt.get("applicants_count") or len(self.applicants) or 1
        return max(1, int(value))

    @property
    def applicants(self) -> list[dict[str, Any]]:
        return self.raw.get("applicants") or []

    @property
    def otp_mode(self) -> str:
        return self.raw.get("otp", {}).get("mode", "manual")

    @property
    def imap_cfg(self) -> dict[str, Any]:
        return self.raw.get("otp", {}).get("imap", {}) or {}

    @property
    def notletters_cfg(self) -> dict[str, Any]:
        cfg = dict(self.raw.get("otp", {}).get("notletters", {}) or {})
        value = str(cfg.get("api_key") or "").strip()
        if value.lower().startswith("env:"):
            env_name = value.split(":", 1)[1].strip()
            value = os.getenv(env_name, "").strip()
        cfg["api_key"] = value or os.getenv("NOTLETTERS_API_KEY", "").strip()
        return cfg

    @property
    def account_pool_enabled(self) -> bool:
        return bool(self.raw.get("account_pool", {}).get("enabled", False))

    @property
    def registration_cfg(self) -> dict[str, Any]:
        return self.raw.get("registration", {}) or {}

    @property
    def registration_enabled(self) -> bool:
        reg = self.registration_cfg
        return bool(reg.get("enabled", False) or reg.get("auto_register", False))

    @property
    def registration_auto_register(self) -> bool:
        return bool(self.registration_cfg.get("auto_register", False))

    @property
    def registration_max_per_run(self) -> int:
        return max(1, int(self.registration_cfg.get("max_per_run", 1)))

    @property
    def telegram_token(self) -> str:
        return (self.raw.get("telegram", {}).get("bot_token") or "").strip()

    @property
    def telegram_chat_id(self) -> str:
        return str(self.raw.get("telegram", {}).get("chat_id") or "").strip()

    @property
    def telegram_heartbeat_every(self) -> int:
        return int(self.raw.get("telegram", {}).get("heartbeat_every", 0))

    @property
    def log_level(self) -> str:
        return self.raw.get("logging", {}).get("level", "INFO").upper()

    @property
    def screenshots_enabled(self) -> bool:
        return bool(self.raw.get("logging", {}).get("screenshots", True))

    @property
    def screenshot_dir(self) -> Path:
        d = self.raw.get("logging", {}).get("screenshot_dir", "screenshots")
        p = Path(d)
        if not p.is_absolute():
            p = Path(__file__).resolve().parent.parent / p
        p.mkdir(parents=True, exist_ok=True)
        return p


_REQUIRED_TOP = ["portal", "account", "appointment", "behaviour"]


def _fail(msg: str) -> None:
    print(f"[config] ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


def load_config(path: Path | None = None) -> Config:
    path = path or CONFIG_PATH
    if not path.exists():
        _fail(
            f"{path} not found. Copy config.yaml.example to config.yaml and edit it."
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:  # pragma: no cover - user error path
        _fail(f"could not parse {path}: {e}")
        return Config()  # unreachable, keeps type-checkers happy

    for key in _REQUIRED_TOP:
        if key not in raw:
            _fail(f"missing top-level section '{key}' in {path}")

    acc = raw["account"]
    pool_enabled = bool(raw.get("account_pool", {}).get("enabled", False))
    if not pool_enabled:
        if not acc.get("email") or "@" not in str(acc.get("email", "")):
            _fail("account.email looks invalid")
        if not acc.get("password") or acc["password"] in {"", "your-vfs-password"}:
            _fail("account.password is not set")
    elif not raw.get("account_pool", {}).get("vfs_password") and (
        not acc.get("password") or acc["password"] in {"", "your-vfs-password"}
    ):
        _fail("account_pool.vfs_password or account.password is required")

    if not raw["portal"].get("url_segment") and not raw["portal"].get("login_url"):
        _fail("portal.url_segment (e.g. 'rus/en/fra') or portal.login_url is required")

    cfg = Config(raw=raw)

    booking_enabled = cfg.auto_book or cfg.auto_book_dry_run
    if booking_enabled and not cfg.applicants:
        print(
            "[config] WARNING: auto_book is true but 'applicants:' is empty — "
            "booking will only work if the portal pre-fills applicant data.",
            file=sys.stderr,
        )
    elif booking_enabled and cfg.applicants_count > len(cfg.applicants):
        print(
            f"[config] WARNING: applicants_count={cfg.applicants_count} but "
            f"only {len(cfg.applicants)} applicant record(s) are configured.",
            file=sys.stderr,
        )
    if not cfg.proxy:
        print(
            "[config] WARNING: no network.proxy set. VFS/Cloudflare often blocks "
            "datacenter IPs (403201). If you get blocked, add a residential proxy.",
            file=sys.stderr,
        )
    return cfg

"""Load and validate config.yaml."""
from __future__ import annotations

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
    def relogin_after_failures(self) -> int:
        return int(self.raw["behaviour"].get("relogin_after_failures", 3))

    @property
    def proxy(self) -> str:
        return (self.raw.get("network", {}).get("proxy") or "").strip()

    @property
    def headless(self) -> bool:
        return bool(self.raw.get("network", {}).get("headless", False))

    @property
    def chrome_version(self) -> str:
        return str(self.raw.get("network", {}).get("chrome_version") or "").strip()

    # --- captcha ----------------------------------------------------------
    @property
    def captcha_provider(self) -> str:
        return str(self.raw.get("captcha", {}).get("provider") or "none").strip().lower()

    @property
    def captcha_api_key(self) -> str:
        return str(self.raw.get("captcha", {}).get("api_key") or "").strip()

    @property
    def captcha_timeout(self) -> int:
        return int(self.raw.get("captcha", {}).get("timeout_seconds", 120))

    @property
    def captcha_enabled(self) -> bool:
        return self.captcha_provider not in ("none", "off", "") and bool(self.captcha_api_key)

    @property
    def appointment(self) -> dict[str, Any]:
        return self.raw["appointment"]

    @property
    def applicants(self) -> list[dict[str, Any]]:
        return self.raw.get("applicants", [])

    @property
    def otp_mode(self) -> str:
        return self.raw.get("otp", {}).get("mode", "manual")

    @property
    def imap_cfg(self) -> dict[str, Any]:
        return self.raw.get("otp", {}).get("imap", {}) or {}

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
    if not acc.get("email") or "@" not in str(acc.get("email", "")):
        _fail("account.email looks invalid")
    if not acc.get("password") or acc["password"] in {"", "your-vfs-password"}:
        _fail("account.password is not set")

    if not raw["portal"].get("url_segment") and not raw["portal"].get("login_url"):
        _fail("portal.url_segment (e.g. 'rus/en/fra') or portal.login_url is required")

    cfg = Config(raw=raw)

    if cfg.auto_book and not cfg.applicants:
        print(
            "[config] WARNING: auto_book is true but 'applicants:' is empty — "
            "booking will only work if the portal pre-fills applicant data.",
            file=sys.stderr,
        )
    if not cfg.proxy:
        print(
            "[config] WARNING: no network.proxy set. VFS/Cloudflare often blocks "
            "datacenter IPs (403201). If you get blocked, add a residential proxy.",
            file=sys.stderr,
        )
    return cfg

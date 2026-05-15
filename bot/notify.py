"""Telegram notifier. No-op (logs only) if telegram.bot_token isn't configured."""
from __future__ import annotations

from pathlib import Path

import requests

from .util import log

_API = "https://api.telegram.org/bot{token}/{method}"


class Notifier:
    def __init__(self, cfg) -> None:
        self.token = cfg.telegram_token
        self.chat_id = cfg.telegram_chat_id
        self.enabled = bool(self.token and self.chat_id)
        if not self.enabled:
            log.info("Telegram not configured — notifications go to the console only.")

    # --- low level ---------------------------------------------------------
    def _post(self, method: str, **data):
        if not self.enabled:
            return None
        try:
            r = requests.post(_API.format(token=self.token, method=method), data=data, timeout=20)
            if r.status_code != 200:
                log.warning("Telegram %s failed: %s %s", method, r.status_code, r.text[:200])
            return r
        except Exception as e:
            log.warning("Telegram %s error: %s", method, e)
            return None

    # --- public ------------------------------------------------------------
    def send(self, text: str, *, silent: bool = False) -> None:
        log.info("[notify] %s", text.replace("\n", " | "))
        self._post(
            "sendMessage",
            chat_id=self.chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            disable_notification=silent,
        )

    def send_photo(self, path: Path | str, caption: str = "") -> None:
        if not self.enabled:
            return
        try:
            with open(path, "rb") as f:
                requests.post(
                    _API.format(token=self.token, method="sendPhoto"),
                    data={"chat_id": self.chat_id, "caption": caption[:1000]},
                    files={"photo": f},
                    timeout=30,
                )
        except Exception as e:
            log.warning("Telegram sendPhoto error: %s", e)

    # --- convenience messages ---------------------------------------------
    def slots_found(self, dates: list[str], note: str) -> None:
        head = "🟢 <b>VFS slots available!</b>"
        body = f"\n{note}"
        if dates:
            shown = ", ".join(dates[:15])
            more = f" (+{len(dates) - 15} more)" if len(dates) > 15 else ""
            body += f"\nDates: {shown}{more}"
        self.send(head + body)

    def booked(self, result) -> None:
        msg = (
            "✅ <b>APPOINTMENT BOOKED!</b>\n"
            f"Date: {result.date or '(unknown)'}\n"
            f"Reference: {result.reference or '(none found — check screenshot)'}\n"
            f"{result.note}"
        )
        self.send(msg)

    def booking_failed(self, result) -> None:
        self.send(
            "⚠️ <b>Tried to book but couldn't confirm it.</b>\n"
            f"Date attempted: {result.date or '(unknown)'}\n{result.note}"
        )

    def need_otp(self, what: str) -> None:
        self.send(
            f"🔐 <b>OTP needed</b> ({what}).\n"
            "The bot is paused waiting for you to type the code in its terminal."
        )

    def error(self, where: str, err: Exception) -> None:
        self.send(f"❌ <b>Error in {where}</b>\n<code>{type(err).__name__}: {err}</code>")

    def heartbeat(self, n: int, last_note: str) -> None:
        self.send(f"💓 still watching (check #{n}). Last: {last_note}", silent=True)

    def started(self, cfg) -> None:
        self.send(
            "▶️ <b>VFS bot started</b>\n"
            f"Portal: {cfg.login_url}\n"
            f"Centre: {cfg.appointment.get('visa_centre')}\n"
            f"Category: {cfg.appointment.get('visa_category')} / {cfg.appointment.get('visa_sub_category')}\n"
            f"Auto-book: {'yes' if cfg.auto_book else 'no'} | interval ~{cfg.check_interval}s",
            silent=True,
        )

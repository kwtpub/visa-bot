"""Retrieve the one-time code VFS sends by email (or SMS).

Two strategies:
  - manual : block and ask the operator to type the code in the terminal.
  - imap   : poll an IMAP mailbox for a recent message from VFS and extract
             the first 4–8 digit code from it.

The IMAP path is best-effort: if it can't find a code in time it falls back
to asking on the console, so the bot never silently stalls.
"""
from __future__ import annotations

import email
import imaplib
import re
import time
from email.header import decode_header
from typing import Any

from .util import log

_CODE_RE = re.compile(r"\b(\d{4,8})\b")


def get_otp(cfg, *, prompt: str = "Enter the OTP code VFS just sent you") -> str:
    mode = (cfg.otp_mode or "manual").lower()
    if mode == "imap":
        code = _get_otp_via_imap(cfg.imap_cfg)
        if code:
            log.info("Got OTP %s from mailbox.", _mask(code))
            return code
        log.warning("Could not read OTP from mailbox in time — falling back to manual input.")
    return _ask_console(prompt)


# --- manual ----------------------------------------------------------------
def _ask_console(prompt: str) -> str:
    print()
    print("=" * 60)
    print(f">>> {prompt}")
    print(">>> (check the email/phone of the VFS account, then type it here)")
    print("=" * 60)
    while True:
        code = input(">>> OTP: ").strip()
        code = re.sub(r"\s+", "", code)
        if code.isdigit() and 4 <= len(code) <= 8:
            return code
        print("    …that doesn't look like a 4–8 digit code, try again.")


# --- IMAP ------------------------------------------------------------------
def _decode(s: Any) -> str:
    if s is None:
        return ""
    if isinstance(s, bytes):
        try:
            return s.decode(errors="replace")
        except Exception:
            return str(s)
    parts = decode_header(s)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def _body_text(msg: email.message.Message) -> str:
    if msg.is_multipart():
        chunks = []
        for part in msg.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                if payload:
                    chunks.append(payload.decode(part.get_content_charset() or "utf-8", errors="replace"))
        return "\n".join(chunks)
    payload = msg.get_payload(decode=True)
    if payload:
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return str(msg.get_payload())


def _get_otp_via_imap(imap_cfg: dict[str, Any]) -> str | None:
    host = imap_cfg.get("host")
    if not host:
        return None
    port = int(imap_cfg.get("port", 993))
    user = imap_cfg.get("user")
    password = imap_cfg.get("password")
    folder = imap_cfg.get("folder", "INBOX")
    from_contains = (imap_cfg.get("from_contains") or "vfsglobal").lower()
    wait_seconds = int(imap_cfg.get("wait_seconds", 120))

    deadline = time.time() + wait_seconds
    started = time.time()
    while time.time() < deadline:
        try:
            with imaplib.IMAP4_SSL(host, port) as M:
                M.login(user, password)
                M.select(folder)
                # Search unseen messages from VFS; if the server is picky about
                # FROM matching, fall back to all recent unseen.
                typ, data = M.search(None, "UNSEEN")
                ids = data[0].split() if data and data[0] else []
                # newest first
                for mid in reversed(ids):
                    typ, msg_data = M.fetch(mid, "(RFC822)")
                    if typ != "OK" or not msg_data or not msg_data[0]:
                        continue
                    raw = msg_data[0][1]
                    msg = email.message_from_bytes(raw)
                    sender = _decode(msg.get("From", "")).lower()
                    if from_contains and from_contains not in sender:
                        continue
                    # only consider mail that arrived around/after we started
                    body = _body_text(msg)
                    subject = _decode(msg.get("Subject", ""))
                    m = _CODE_RE.search(subject) or _CODE_RE.search(body)
                    if m:
                        # mark as seen so we don't re-pick it next time
                        try:
                            M.store(mid, "+FLAGS", "\\Seen")
                        except Exception:
                            pass
                        return m.group(1)
        except Exception as e:
            log.debug("IMAP poll error: %s", e)
        # wait before polling again
        if time.time() - started > 5:
            log.info("Waiting for OTP email… (%.0fs left)", deadline - time.time())
        time.sleep(5)
    return None


def _mask(code: str) -> str:
    if len(code) <= 2:
        return "*" * len(code)
    return code[0] + "*" * (len(code) - 2) + code[-1]


def fill_otp_into_page(sb, code: str, input_selectors, submit_selectors) -> bool:
    """Type `code` into whatever OTP field is on the page and submit.

    Handles both a single input and the split 6-box variant. Returns True if it
    found a field to type into.
    """
    from .util import by_of, first_visible, human_pause

    sel = first_visible(sb, input_selectors, timeout=8)
    if not sel:
        return False
    by = by_of(sel)
    # Split-box variant: multiple inputs match -> type one digit each.
    try:
        elements = sb.find_elements(sel, by=by)
    except Exception:
        elements = []
    if len(elements) > 1 and len(elements) >= len(code):
        for el, ch in zip(elements, code):
            el.clear()
            el.send_keys(ch)
            human_pause(0.05, 0.2)
    else:
        sb.clear(sel, by=by)
        # type with tiny delays
        for ch in code:
            sb.send_keys(sel, ch, by=by)
            human_pause(0.03, 0.12)
    human_pause()
    sub = first_visible(sb, submit_selectors, timeout=3)
    if sub:
        sb.click(sub, by=by_of(sub))
    else:
        # some forms auto-submit when all digits are entered
        sb.send_keys(sel, "\n", by=by)
    return True

# visa-frans-bot — VFS Global appointment monitor + auto-booker

Monitors appointment availability on **VFS Global** (`https://visa.vfsglobal.com/<country>/<lang>/fra/login`)
for a French Schengen visa, notifies you (Telegram / console), and — optionally —
**tries to book the slot automatically**, pausing for OTP codes.

Built on **[SeleniumBase](https://seleniumbase.io/) UC mode** (undetected Chrome), which
is currently the most reliable way to get past the Cloudflare **Turnstile** widget on the
VFS login page.

---

## ⚠️ Read this before you start — it's not plug-and-play

VFS deliberately made automation hard because thousands of bots exist. Realistic expectations:

1. **You almost certainly need a residential / mobile proxy** in the country you apply from.
   Cloudflare blocks datacenter & hosting IPs at the edge (`HTTP 403`, code `403201`).
   From a home connection it *may* work; from a VPS it won't. Configure `network.proxy`.
2. **Accounts get rate-limited and banned for hammering.** Keep `check_interval_seconds`
   at **10–20 minutes**. The bot adds random jitter on top. Don't get greedy.
3. **OTP is a manual step unless you wire IMAP.** VFS emails a code at login (and sometimes
   before confirming a booking). In `manual` mode the bot prints `>>> ENTER OTP:` and waits
   for you to type it. In `imap` mode it reads it from your mailbox automatically.
4. **The site changes.** Selectors (centre/category dropdowns, calendar, buttons) drift.
   When something breaks, run with `--inspect` to dump current options, and update
   `bot/selectors.py`.
5. **This is for your own appointment.** Don't run a slot-reselling operation. Respect
   VFS's terms — using this is at your own risk.

If you just want it to *work* with zero fuss, paid managed services exist (e.g. Opaige).
This repo is the DIY route.

---

## Install

```bash
git clone <this repo>           # or just copy the folder
cd visa-frans-bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
sbase install chromedriver       # SeleniumBase fetches a matching driver
```

## Configure

```bash
cp config.yaml.example config.yaml
$EDITOR config.yaml
```

Fill in at least: `account.email`, `account.password`, `portal.url_segment`
(`rus/en/fra` for Russia→France), and — once you know them — the exact
`appointment.visa_centre / visa_category / visa_sub_category` strings.

To discover those exact strings, log in once with the bot in inspect mode:

```bash
python -m bot.main --inspect
```

It will print every option in the dropdowns. Copy them verbatim into `config.yaml`.

## Run

```bash
# Monitor only (notify, never book) — set behaviour.auto_book: false in config, or:
python -m bot.main --no-book

# Monitor + auto-book (uses behaviour.auto_book from config)
python -m bot.main

# One-shot: check once and exit (good for cron)
python -m bot.main --once

# Watch what it does (overrides headless)
python -m bot.main --show
```

## Cloudflare Turnstile (paid solver)

The free `sb.uc_gui_click_captcha()` helper sometimes can't get past Turnstile
on VFS — especially in headless mode or after a few retries. Configure a paid
solver as a fallback:

1. Sign up at https://capsolver.com and top up a few dollars (~$0.8 / 1000
   solves on Turnstile).
2. Copy the API key from the dashboard.
3. Edit `config.yaml`:
   ```yaml
   captcha:
     provider: "capsolver"
     api_key: "CAP-XXXXXXXX..."
     timeout_seconds: 120
   ```

The bot will:
1. First try the built-in UC click (free).
2. If that doesn't clear the widget after 2 attempts, extract the Turnstile
   `sitekey` from the page, send it to CapSolver, and inject the returned
   token into the form.
3. If everything fails and you're running with `--show`, fall back to
   manual click.

Set `provider: "none"` to disable the paid path entirely.

## How it works

```
main.py
  └─ login.py     open login URL ─► pass Turnstile (uc_gui_click_captcha)
  │                ─► type email/password ─► handle login OTP ─► session ready
  ├─ monitor.py   pick centre / category ─► open calendar ─► read free dates
  │                (handles the queue / "waiting room" page; retries politely)
  ├─ booking.py   pick date+time ─► fill applicant data ─► review
  │                ─► handle confirm OTP ─► confirm ─► screenshot the confirmation
  ├─ otp.py       manual console input OR IMAP mailbox polling
  └─ notify.py    Telegram messages (found / booked / error / OTP-needed / heartbeat)
```

State machine per cycle:
`LOGIN → CHECK → (no slot ⇒ sleep, loop) | (slot ⇒ BOOK → OTP? → CONFIRMED ⇒ notify & maybe stop)`

## Files

| File | Purpose |
|---|---|
| `bot/main.py` | CLI, config load, main loop, retry/relogin logic |
| `bot/login.py` | login flow + Turnstile + login-OTP |
| `bot/monitor.py` | navigate to calendar, parse availability, handle queue page |
| `bot/booking.py` | the actual booking steps + confirm-OTP |
| `bot/otp.py` | OTP retrieval (manual / IMAP) |
| `bot/notify.py` | Telegram notifier (no-op if not configured) |
| `bot/selectors.py` | **all CSS/XPath selectors in one place** — edit here when the site changes |
| `bot/config.py` | load + validate `config.yaml` |
| `config.yaml.example` | annotated config template |

## Troubleshooting

- **`403` / blank page / "Access denied"** → your IP is blocked. Use a residential proxy in
  the applicant's country (`network.proxy`).
- **Turnstile never passes** → run with `--show`, solve it by hand once; sometimes a fresh
  proxy IP or `network.chrome_version` pin helps. Make sure `seleniumbase` is up to date.
- **"Login failed" after a few runs** → you're rate-limited. Wait 1–2 hours, increase
  `check_interval_seconds`.
- **Dropdown values "not found"** → run `--inspect`, copy the exact strings.
- **Stuck on a queue page forever** → that's VFS load; the bot waits up to ~5 min then
  retries. Nothing to fix, just be patient.

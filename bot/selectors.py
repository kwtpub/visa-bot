"""All site-specific selectors live here.

VFS Global is an Angular SPA and it changes its markup periodically. When the
bot breaks ("element not found"), this is almost always the file to update.
Run `python -m bot.main --inspect` to dump current page state and adjust.

Each entry is a list of candidate selectors tried in order — the first one that
matches wins. This makes the bot resilient to minor markup tweaks and to A/B
variants. Prefer stable attributes (formcontrolname, id, aria-label) over
generated class names.
"""

# ---------------------------------------------------------------------------
# Cloudflare Turnstile (challenge widget on the login page)
# ---------------------------------------------------------------------------
# SeleniumBase's uc_gui_click_captcha() handles this automatically; these are
# only used as fallbacks / for detection.
TURNSTILE_IFRAME = [
    'iframe[src*="challenges.cloudflare.com"]',
    'iframe[title*="Cloudflare"]',
    'iframe[id^="cf-chl-widget-"]',
]
TURNSTILE_CHECKBOX = [
    'input[type="checkbox"]',
    'label.ctp-checkbox-label',
    '#challenge-stage input',
]
# Page-level signals that we're blocked at the edge (vs. a solvable challenge).
EDGE_BLOCK_TEXTS = [
    "Sorry, you have been blocked",
    "error code: 1020",
    "403201",
    "Access denied",
]

# ---------------------------------------------------------------------------
# Login form
# ---------------------------------------------------------------------------
LOGIN_EMAIL = [
    'input[formcontrolname="username"]',
    'input#email',
    'input[type="email"]',
    'input[name="username"]',
]
LOGIN_PASSWORD = [
    'input[formcontrolname="password"]',
    'input#password',
    'input[type="password"]',
    'input[name="password"]',
]
LOGIN_SUBMIT = [
    'button[type="submit"]',
    'button.mat-focus-indicator.btn-block',
    '//button[contains(., "Sign In") or contains(., "Sign in") or contains(., "Login")]',
]
# Shown after a wrong password / rate limit.
LOGIN_ERROR = [
    'div.alert-danger',
    'mat-error',
    '.error-message',
    '//*[contains(text(), "incorrect") or contains(text(), "Invalid") or contains(text(), "try again later")]',
]

# ---------------------------------------------------------------------------
# OTP entry (email/SMS code, after login and sometimes before confirming)
# ---------------------------------------------------------------------------
OTP_INPUT = [
    'input[formcontrolname="otp"]',
    'input#otp',
    'input[name="otp"]',
    'input[autocomplete="one-time-code"]',
    # Some flows split into 6 single-char boxes:
    'input.otp-box, input[formcontrolname^="digit"]',
]
OTP_SUBMIT = [
    'button[type="submit"]',
    '//button[contains(., "Verify") or contains(., "Submit") or contains(., "Continue")]',
]
OTP_RESEND = [
    '//a[contains(., "Resend")]',
    '//button[contains(., "Resend")]',
]

# ---------------------------------------------------------------------------
# "Start new booking" / dashboard
# ---------------------------------------------------------------------------
START_BOOKING_BTN = [
    '//button[contains(., "Start New Booking") or contains(., "Book Appointment") or contains(., "New Booking")]',
    'a[href*="schedule-appointment"]',
    'button.btn-book',
]

# ---------------------------------------------------------------------------
# Appointment selection — these are mat-select dropdowns (Angular Material).
# Pattern: click the trigger, then click the option with the matching text.
# ---------------------------------------------------------------------------
# Triggers (the visible dropdown box):
SELECT_CENTRE_TRIGGER = [
    'mat-select[formcontrolname="centre"]',
    'mat-select[formcontrolname="visaApplicationCentre"]',
    '#mat-select-0',
    '//mat-label[contains(., "Centre") or contains(., "Center")]/ancestor::mat-form-field//mat-select',
]
SELECT_CATEGORY_TRIGGER = [
    'mat-select[formcontrolname="category"]',
    'mat-select[formcontrolname="visaCategory"]',
    '//mat-label[contains(., "Category")]/ancestor::mat-form-field//mat-select',
]
SELECT_SUBCATEGORY_TRIGGER = [
    'mat-select[formcontrolname="subCategory"]',
    'mat-select[formcontrolname="visaSubCategory"]',
    '//mat-label[contains(., "Sub") or contains(., "Sub-category") or contains(., "Sub category")]/ancestor::mat-form-field//mat-select',
]
# The popup panel that opens, and an individual option inside it:
MAT_OPTION_PANEL = ['div.cdk-overlay-pane', 'div.mat-select-panel', 'div[role="listbox"]']
MAT_OPTION_ANY = ['mat-option', 'mat-option span.mat-option-text', '[role="option"]']
# Build at runtime: f'mat-option//span[normalize-space()="{value}"]'

CONTINUE_BTN = [
    '//button[contains(., "Continue") or contains(., "Submit") or contains(., "Proceed") or contains(., "Next")]',
    'button[type="submit"]',
]

# ---------------------------------------------------------------------------
# Availability / calendar
# ---------------------------------------------------------------------------
# Message that means "no slots":
NO_SLOTS_TEXT = [
    "no appointment slots",
    "No slots available",
    "currently no appointments",
    "no dates available",
    "appointments are not available",
]
# Calendar widget + day cells that are selectable (free) vs disabled:
CALENDAR_ROOT = ['mat-calendar', '.mat-calendar', 'div.calendar']
CALENDAR_DAY_AVAILABLE = [
    'button.mat-calendar-body-cell:not(.mat-calendar-body-disabled)',
    'td.available a',
    '.calendar-day.available',
]
CALENDAR_DAY_DISABLED = [
    'button.mat-calendar-body-cell.mat-calendar-body-disabled',
    'td.disabled',
]
CALENDAR_NEXT_MONTH = [
    'button.mat-calendar-next-button:not([disabled])',
    'button[aria-label="Next month"]',
]
# Time-slot list shown after a day is picked:
TIME_SLOT_AVAILABLE = [
    'button.time-slot:not([disabled])',
    'mat-radio-button:not(.mat-radio-disabled)',
    '.slot.available',
    '//button[contains(@class, "slot") and not(@disabled)]',
]

# ---------------------------------------------------------------------------
# Queue / "waiting room" page
# ---------------------------------------------------------------------------
QUEUE_PAGE_TEXTS = [
    "You are now in line",
    "waiting room",
    "Your estimated wait time",
    "high demand",
    "please wait while we",
    "queue-it",  # Queue-it is the vendor VFS uses
]
QUEUE_PAGE_HOSTS = ["queue-it.net", "vfsglobal.queue-it.net"]

# ---------------------------------------------------------------------------
# Applicant details form (auto-booking only). Field names vary a lot by portal;
# the bot fills whichever of these it finds and leaves the rest for you.
# ---------------------------------------------------------------------------
APPLICANT_FIELDS = {
    "first_name": ['input[formcontrolname="firstName"]', 'input[name="firstName"]', '#firstName'],
    "last_name": ['input[formcontrolname="lastName"]', 'input[name="lastName"]', '#lastName'],
    "passport_number": ['input[formcontrolname="passportNumber"]', 'input[name="passportNumber"]', '#passportNumber'],
    "passport_expiry": ['input[formcontrolname="passportExpiryDate"]', 'input[name="passportExpiry"]', '#passportExpiry'],
    "date_of_birth": ['input[formcontrolname="dateOfBirth"]', 'input[name="dateOfBirth"]', '#dateOfBirth'],
    "phone_number": ['input[formcontrolname="contactNumber"]', 'input[name="phone"]', '#phone'],
    "email": ['input[formcontrolname="email"]', 'input[name="email"]', '#email'],
}
APPLICANT_GENDER_SELECT = ['mat-select[formcontrolname="gender"]', 'select[name="gender"]']
APPLICANT_NATIONALITY_SELECT = ['mat-select[formcontrolname="nationality"]', 'select[name="nationality"]']

# Final review / confirm:
REVIEW_CONFIRM_BTN = [
    '//button[contains(., "Confirm") or contains(., "Pay") or contains(., "Book Appointment") or contains(., "Confirm Booking")]',
    'button.btn-confirm',
]
BOOKING_SUCCESS_TEXTS = [
    "Appointment confirmed",
    "successfully booked",
    "Your appointment has been booked",
    "booking reference",
    "appointment is confirmed",
]

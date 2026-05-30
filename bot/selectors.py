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
# Used for Turnstile detection and token injection. GUI captcha click helpers
# are intentionally not used because they move the real OS mouse.
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
    "429201",
    "Access denied",
    "Account blocked",
    "Аккаунт заблокирован",
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
    'button.btn-block.btn-brand-orange',
    'button.mat-focus-indicator.btn-block',
    'button.btn-block.mdc-button',
    '//button[contains(., "Sign In") or contains(., "Sign in") or contains(., "Login")]',
    # Russian portals (rus/ru/*) display text in Russian:
    '//button[contains(., "Войти") or contains(., "Вход")]',
]
# Shown after a wrong password / rate limit.
LOGIN_ERROR = [
    'div.alert-danger',
    'mat-error',
    '.error-message',
    '//*[contains(text(), "incorrect") or contains(text(), "Invalid") or contains(text(), "try again later")]',
    # Russian error messages:
    '//*[contains(text(), "неверн") or contains(text(), "Ошибка") or contains(text(), "попробуйте позже")]',
]

# ---------------------------------------------------------------------------
# Cookie consent overlay (OneTrust) — blocks form interaction if not dismissed
# ---------------------------------------------------------------------------
# Registration form
REGISTER_LINK = [
    '//a[contains(., "Create an account") or contains(., "Create Account") or contains(., "Sign Up") or contains(., "Sign up") or contains(., "Register") or contains(., "New User")]',
    '//button[contains(., "Create an account") or contains(., "Create Account") or contains(., "Sign Up") or contains(., "Sign up") or contains(., "Register") or contains(., "New User")]',
    '//*[(@role="button" or self::a or self::button) and contains(normalize-space(), "\u0423 \u043c\u0435\u043d\u044f \u043d\u0435\u0442 \u0430\u043a\u043a\u0430\u0443\u043d\u0442\u0430")]',
    '//*[(@role="button" or self::a or self::button) and contains(normalize-space(), "\u041d\u0435\u0442 \u0430\u043a\u043a\u0430\u0443\u043d\u0442\u0430")]',
    '//*[(@role="button" or self::a or self::button) and contains(normalize-space(), "\u0417\u0430\u0440\u0435\u0433\u0438\u0441\u0442\u0440")]',
    '//*[(@role="button" or self::a or self::button) and contains(normalize-space(), "\u0421\u043e\u0437\u0434\u0430\u0442\u044c")]',
]
REGISTER_EMAIL = [
    'input[formcontrolname="emailid"]',
    'input[formcontrolname="email"]',
    'input#inputEmail',
    'input[type="email"]',
]
REGISTER_PASSWORD = [
    'input[formcontrolname="password"]',
    'input#password',
    'input[type="password"]',
]
REGISTER_CONFIRM_PASSWORD = [
    'input[formcontrolname="confirmPassword"]',
    'input#confirmPassword',
    'input[name="confirmPassword"]',
]
REGISTER_DIAL_CODE = [
    'input[formcontrolname="dialcode"]',
    'input[formcontrolname="dialCode"]',
    'select[formcontrolname="dialcode"]',
    'select[formcontrolname="dialCode"]',
    'mat-select[formcontrolname="dialcode"]',
    'mat-select[formcontrolname="dialCode"]',
    'ng-select[formcontrolname="dialcode"] input',
    'ng-select[formcontrolname="dialCode"] input',
]
REGISTER_PHONE = [
    'input[formcontrolname="contact"]',
    'input[formcontrolname="contactNumber"]',
    'input[formcontrolname="phoneNumber"]',
    'input[name="phone"]',
]
REGISTER_FIRST_NAME = [
    'input[formcontrolname="firstName"]',
    'input[name="firstName"]',
    'input#firstName',
]
REGISTER_LAST_NAME = [
    'input[formcontrolname="lastName"]',
    'input[name="lastName"]',
    'input#lastName',
]
REGISTER_DOB = [
    'input[formcontrolname="dateOfBirth"]',
    'input[name="dateOfBirth"]',
    'input#dateOfBirth',
]
REGISTER_PASSPORT_NUMBER = [
    'input[formcontrolname="passportNumber"]',
    'input[name="passportNumber"]',
    'input#passportNumber',
]
REGISTER_NATIONALITY_SELECT = [
    'mat-select[formcontrolname="nationality"]',
    'select[formcontrolname="nationality"]',
    'ng-select[formcontrolname="nationality"] input',
]
REGISTER_CHECKBOX_CONTROLS = [
    "processPerDataAgreed",
    "intTransPerDataAgreed",
    "termAndConditionAgreed",
]
REGISTER_SUBMIT = [
    '//button[contains(., "Register") or contains(., "Create") or contains(., "Submit") or contains(., "Continue")]',
    '//button[contains(., "\u0417\u0430\u0440\u0435\u0433") or contains(., "\u0421\u043e\u0437\u0434\u0430") or contains(., "\u041f\u0440\u043e\u0434\u043e\u043b\u0436")]',
    'button[type="submit"]',
    'button[id*="submit"]',
]
REGISTER_ERROR = [
    'div.alert-danger',
    'div.alert-error',
    'mat-error',
    '.error-message',
    '.c-brand-error',
    '//*[self::mat-error or contains(@class, "error") or contains(@class, "alert")][contains(text(), "already") or contains(text(), "exists") or contains(text(), "Invalid")]',
    '//*[self::mat-error or contains(@class, "error") or contains(@class, "alert")][contains(text(), "\u0443\u0436\u0435") or contains(text(), "\u043e\u0448\u0438\u0431") or contains(text(), "\u043d\u0435\u0432\u0435\u0440")]',
]
REGISTER_SUCCESS_TEXTS = [
    "registered successfully",
    "registration successful",
    "account has been created",
    "activation link",
    "activation email",
    "email has been sent",
    "\u0443\u0441\u043f\u0435\u0448\u043d\u043e",
    "\u0441\u0441\u044b\u043b\u043a",
    "\u0430\u043a\u0442\u0438\u0432",
]
REGISTER_ALREADY_EXISTS_TEXTS = [
    "already registered",
    "already exists",
    "user exists",
    "email already",
    "\u0443\u0436\u0435 \u0437\u0430\u0440\u0435\u0433",
    "\u0443\u0436\u0435 \u0441\u0443\u0449\u0435\u0441\u0442",
]
REGISTER_ACTIVATED_TEXTS = [
    "account activated",
    "email verified",
    "activation successful",
    "activated successfully",
    "\u0430\u043a\u0442\u0438\u0432\u0438\u0440",
    "\u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434",
]
ACTIVATION_EMAIL = [
    'input[formcontrolname="emailid"]',
    'input[formcontrolname="email"]',
    'input[type="email"]',
    'input#email',
]
ACTIVATION_SUBMIT = [
    '//button[contains(., "Activate") or contains(., "Submit") or contains(., "Continue")]',
    '//button[contains(., "\u0410\u043a\u0442\u0438\u0432\u0438\u0440\u043e\u0432\u0430\u0442\u044c") or contains(., "\u041f\u0440\u043e\u0434\u043e\u043b\u0436")]',
    'button[type="submit"]',
    'button.btn-brand-orange',
]

# Cookie consent overlay (OneTrust)
COOKIE_ACCEPT_BTN = [
    '#onetrust-accept-btn-handler',           # "Accept All Cookies"
    '#onetrust-reject-all-handler',           # "Accept Only Necessary" (also works)
    '//button[contains(., "Accept All")]',
    '//button[contains(., "Accept Only Necessary")]',
]
COOKIE_BANNER = [
    '#onetrust-banner-sdk',
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
    '//button[contains(normalize-space(), "\u0417\u0430\u043f\u0438\u0441")]',
    '//*[(@role="button" or self::a) and contains(normalize-space(), "\u0417\u0430\u043f\u0438\u0441")]',
    '//*[self::button or self::a][contains(., "\u0417\u0430\u043f\u0438\u0441\u0430\u0442\u044c\u0441\u044f \u043d\u0430 \u043f\u0440\u0438\u0435\u043c") or contains(., "\u0417\u0430\u043f\u0438\u0441\u0430\u0442\u044c\u0441\u044f \u043d\u0430 \u043f\u0440\u0438\u0451\u043c")]',
    'a[href*="schedule-appointment"]',
    'button.btn-book',
    'button.btn-brand-orange',
    'button[class*="btn-brand-orange"]',
]

# ---------------------------------------------------------------------------
# Appointment selection — these are mat-select dropdowns (Angular Material).
# Pattern: click the trigger, then click the option with the matching text.
# ---------------------------------------------------------------------------
# Triggers (the visible dropdown box):
SELECT_CENTRE_TRIGGER = [
    'mat-select[formcontrolname="centerCode"]',
    'mat-select[formcontrolname="centre"]',
    'mat-select[formcontrolname="visaApplicationCentre"]',
    '#mat-select-0',
    '//mat-label[contains(., "Centre") or contains(., "Center") or contains(., "\u0426\u0435\u043d\u0442\u0440")]/ancestor::mat-form-field//mat-select',
    '//mat-label[contains(., "\u0446\u0435\u043d\u0442\u0440") or contains(., "\u0426\u0435\u043d\u0442\u0440")]/ancestor::mat-form-field//mat-select',
]
SELECT_CATEGORY_TRIGGER = [
    'mat-select[formcontrolname="selectedSubvisaCategory"]',
    'mat-select[formcontrolname="category"]',
    'mat-select[formcontrolname="visaCategory"]',
    '#mat-select-2',
    '//mat-label[contains(., "Category") or contains(., "\u043a\u0430\u0442\u0435\u0433\u043e\u0440\u0438\u044e \u0437\u0430\u043f\u0438\u0441\u0438")]/ancestor::mat-form-field//mat-select',
    '//mat-label[contains(., "\u041a\u0430\u0442\u0435\u0433\u043e\u0440") or contains(., "\u043a\u0430\u0442\u0435\u0433\u043e\u0440")]/ancestor::mat-form-field//mat-select',
]
SELECT_SUBCATEGORY_TRIGGER = [
    'mat-select[formcontrolname="visaCategoryCode"]',
    'mat-select[formcontrolname="subCategory"]',
    'mat-select[formcontrolname="visaSubCategory"]',
    '#mat-select-1',
    '//mat-label[contains(., "Sub") or contains(., "Sub-category") or contains(., "Sub category") or contains(., "\u043f\u043e\u0434\u043a\u0430\u0442\u0435\u0433\u043e\u0440\u0438\u044e")]/ancestor::mat-form-field//mat-select',
    '//mat-label[contains(., "\u041f\u043e\u0434\u043a\u0430\u0442") or contains(., "\u043f\u043e\u0434\u043a\u0430\u0442")]/ancestor::mat-form-field//mat-select',
]
# The popup panel that opens, and an individual option inside it:
MAT_OPTION_PANEL = ['div.cdk-overlay-pane', 'div.mat-select-panel', 'div[role="listbox"]']
MAT_OPTION_ANY = ['mat-option', 'mat-option span.mat-option-text', '[role="option"]']
# Build at runtime: f'mat-option//span[normalize-space()="{value}"]'

CONTINUE_BTN = [
    '//button[contains(., "Continue") or contains(., "Submit") or contains(., "Proceed") or contains(., "Next")]',
    '//button[contains(., "\u041f\u0440\u043e\u0434\u043e\u043b\u0436\u0438\u0442\u044c")]',
    '//*[self::button or self::a][normalize-space()="\u041f\u0440\u043e\u0434\u043e\u043b\u0436\u0438\u0442\u044c"]',
]

APPOINTMENT_CAPTCHA_TEXTS = [
    "\u041f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u0435 \u043a\u0430\u043f\u0447\u0443",
    "Verify you are human",
]
CAPTCHA_SUBMIT_BTN = [
    '//mat-dialog-container//button[normalize-space()="Submit"]',
    '//mat-dialog-container//button[contains(., "Submit")]',
    'mat-dialog-container button[type="submit"]',
    '//button[normalize-space()="Submit"]',
    '//button[contains(., "Submit")]',
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
    # Current rus/ru/svn application-detail message:
    "\u043d\u0435\u0442 \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u044b\u0445 \u0441\u043b\u043e\u0442\u043e\u0432",
]
# On the current VFS application-detail page, selecting a category can show
# the nearest slot before the flow enters applicant details / the calendar.
NEAREST_SLOT_TEXTS = [
    "Nearest available slot",
    "Next available slot",
    "\u0411\u043b\u0438\u0436\u0430\u0439\u0448\u0438\u0439 \u0434\u043e\u0441\u0442\u0443\u043f\u043d\u044b\u0439 \u0441\u043b\u043e\u0442",
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
    "first_name": [
        'input[formcontrolname="firstName"]',
        'input[name="firstName"]',
        '#firstName',
        '//app-dynamic-control[contains(., "\u0418\u043c\u044f") and not(contains(., "\u0424\u0430\u043c\u0438\u043b"))]//input',
        'input[placeholder*="\u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u0441\u0432\u043e\u0435 \u0438\u043c\u044f"]',
        'input[placeholder*="\u0438\u043c\u044f"]',
        '#mat-input-3',
    ],
    "last_name": [
        'input[formcontrolname="lastName"]',
        'input[name="lastName"]',
        '#lastName',
        '//app-dynamic-control[contains(., "\u0424\u0430\u043c\u0438\u043b")]//input',
        'input[placeholder*="\u041f\u043e\u0436\u0430\u043b\u0443\u0439\u0441\u0442\u0430, \u0432\u0432\u0435\u0434\u0438\u0442\u0435 \u0444\u0430\u043c\u0438\u043b\u0438\u044e"]',
        'input[placeholder*="\u0444\u0430\u043c\u0438\u043b"]',
        '#mat-input-4',
    ],
    "passport_number": [
        'input[formcontrolname="passportNumber"]',
        'input[name="passportNumber"]',
        '#passportNumber',
        '//app-dynamic-control[contains(., "\u041d\u043e\u043c\u0435\u0440 \u043f\u0430\u0441\u043f\u043e\u0440\u0442\u0430")]//input',
        'input[placeholder*="\u0412\u0432\u0435\u0434\u0438\u0442\u0435 \u043d\u043e\u043c\u0435\u0440 \u043f\u0430\u0441\u043f\u043e\u0440\u0442\u0430"]',
        'input[placeholder*="\u043d\u043e\u043c\u0435\u0440 \u043f\u0430\u0441\u043f\u043e\u0440\u0442"]',
        '#mat-input-5',
    ],
    "passport_expiry": ['input[formcontrolname="passportExpiryDate"]', 'input[name="passportExpiry"]', '#passportExpiry'],
    "date_of_birth": [
        'input[formcontrolname="dateOfBirth"]',
        'input[name="dateOfBirth"]',
        '#dateOfBirth',
        '//app-dynamic-control[contains(., "\u0414\u0430\u0442\u0430 \u0440\u043e\u0436\u0434\u0435\u043d\u0438\u044f")]//input',
        'input[placeholder*="\u0414\u0414"]',
    ],
    "phone_country_code": [
        'input[formcontrolname="phoneCountryCode"]',
        'input[name="phoneCountryCode"]',
        '//app-dynamic-control[contains(., "\u041a\u043e\u043d\u0442\u0430\u043a\u0442\u043d\u044b\u0439 \u043d\u043e\u043c\u0435\u0440")]//input[@maxlength="3"]',
        '#mat-input-6',
    ],
    "phone_number": [
        'input[formcontrolname="contactNumber"]',
        'input[name="phone"]',
        '#phone',
        '//app-dynamic-control[contains(., "\u041a\u043e\u043d\u0442\u0430\u043a\u0442\u043d\u044b\u0439 \u043d\u043e\u043c\u0435\u0440")]//input[@maxlength="15"]',
        '#mat-input-7',
    ],
    "email": [
        'input[formcontrolname="email"]',
        'input[name="email"]',
        '#email',
        'input[type="email"]',
        'input[placeholder*="\u0430\u0434\u0440\u0435\u0441 \u044d\u043b\u0435\u043a\u0442\u0440\u043e\u043d\u043d\u043e\u0439 \u043f\u043e\u0447\u0442\u044b"]',
        '#mat-input-8',
    ],
}
APPLICANT_GENDER_SELECT = [
    'mat-select[formcontrolname="gender"]',
    'select[name="gender"]',
    '//app-dynamic-control[contains(., "\u041f\u043e\u043b")]//mat-select',
    '//app-dynamic-control[contains(., "\u043f\u043e\u043b")]//mat-select',
    '#mat-select-3',
]
APPLICANT_NATIONALITY_SELECT = [
    'mat-select[formcontrolname="nationality"]',
    'select[name="nationality"]',
    '//app-dynamic-control[contains(., "\u0433\u0440\u0430\u0436\u0434\u0430\u043d\u0441\u0442\u0432\u043e")]//mat-select',
    '//app-dynamic-control[contains(., "\u0413\u0440\u0430\u0436\u0434\u0430\u043d\u0441\u0442\u0432\u043e")]//mat-select',
    '#mat-select-4',
]
YOUR_DETAILS_PAGE = [
    '#dateOfBirth',
    '//app-dynamic-control[contains(., "\u041d\u043e\u043c\u0435\u0440 \u043f\u0430\u0441\u043f\u043e\u0440\u0442\u0430")]',
]
YOUR_DETAILS_SAVE_BTN = [
    '//button[contains(., "Save")]',
    '//button[contains(., "\u0421\u043e\u0445\u0440\u0430\u043d\u0438\u0442\u044c")]',
    '//button[contains(., "Update")]',
    '//button[contains(., "\u041e\u0431\u043d\u043e\u0432\u0438\u0442\u044c")]',
]
ADD_APPLICANT_BTN = [
    '//button[contains(., "Add Applicant") or contains(., "Add applicant") or contains(., "Add another applicant")]',
    '//a[contains(., "Add Applicant") or contains(., "Add applicant") or contains(., "Add another applicant")]',
    '//button[contains(., "\u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u0437\u0430\u044f\u0432\u0438\u0442\u0435\u043b\u044f") or contains(., "\u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u0435\u0449\u0435") or contains(., "\u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u0435\u0449\u0451")]',
    '//a[contains(., "\u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u0437\u0430\u044f\u0432\u0438\u0442\u0435\u043b\u044f") or contains(., "\u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u0435\u0449\u0435") or contains(., "\u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u0435\u0449\u0451")]',
]

# Final review / confirm:
REVIEW_CONFIRM_BTN = [
    '//button[contains(., "Confirm") or contains(., "Pay") or contains(., "Book Appointment") or contains(., "Confirm Booking")]',
    '//button[contains(., "\u041f\u043e\u0434\u0442\u0432\u0435\u0440\u0434") or contains(., "\u041e\u043f\u043b\u0430\u0442") or contains(., "\u0417\u0430\u0431\u0440\u043e\u043d\u0438\u0440")]',
    '//button[contains(., "\u0417\u0430\u043f\u0438\u0441\u0430\u0442\u044c\u0441\u044f")]',
    'button.btn-confirm',
]
BOOKING_SUCCESS_TEXTS = [
    "Appointment confirmed",
    "successfully booked",
    "Your appointment has been booked",
    "booking reference",
    "appointment is confirmed",
    "\u0417\u0430\u043f\u0438\u0441\u044c \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0430",
    "\u0443\u0441\u043f\u0435\u0448\u043d\u043e \u0437\u0430\u0431\u0440\u043e\u043d\u0438\u0440",
    "\u043d\u043e\u043c\u0435\u0440 \u0431\u0440\u043e\u043d\u0438\u0440\u043e\u0432\u0430\u043d\u0438\u044f",
]

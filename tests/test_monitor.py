from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from bot.monitor import (
    _application_detail_slot_dates,
    _clear_appointment_turnstile,
    _filter_by_window,
    _go_to_booking_page,
)


class FakeSB:
    def __init__(self, source: str):
        self.source = source

    def get_page_source(self) -> str:
        return self.source


class FakeBookingSB:
    def __init__(self, js_click: bool = False):
        self.form_present = False
        self.clicked = []
        self.js_click = js_click

    def get_page_source(self) -> str:
        return ""

    def is_element_present(self, selector: str, by: str = "css selector") -> bool:
        return self.form_present and selector == "#centre"

    def is_element_visible(self, selector: str, by: str = "css selector") -> bool:
        return (selector == "#start" and not self.js_click) or self.is_element_present(selector, by=by)

    def click(self, selector: str, by: str = "css selector") -> None:
        self.clicked.append((selector, by))
        self.form_present = True

    def execute_script(self, script: str):
        if self.js_click:
            self.clicked.append(("js", "script"))
            self.form_present = True
            return "book appointment"
        return ""


def test_application_detail_slot_date_ru_banner():
    src = (
        "<div>\u0411\u043b\u0438\u0436\u0430\u0439\u0448\u0438\u0439 "
        "\u0434\u043e\u0441\u0442\u0443\u043f\u043d\u044b\u0439 "
        "\u0441\u043b\u043e\u0442 \u0434\u043b\u044f 1 "
        "\u0437\u0430\u044f\u0432\u0438\u0442\u0435\u043b\u0435\u0439 : "
        "25/05/2026</div>"
    )
    assert _application_detail_slot_dates(FakeSB(src)) == ["2026-05-25"]


def test_application_detail_slot_date_absent_without_banner():
    assert _application_detail_slot_dates(FakeSB("<div>25/05/2026</div>")) is None


def test_filter_by_window_drops_nearest_slot_outside_window():
    cfg = SimpleNamespace(appointment={"earliest_date": "2026-06-01", "latest_date": ""})
    assert _filter_by_window(["2026-05-25"], cfg) == []


def test_filter_by_window_excludes_dates():
    cfg = SimpleNamespace(appointment={
        "earliest_date": "",
        "latest_date": "",
        "excluded_dates": ["2026-05-25"],
    })
    assert _filter_by_window(["2026-05-25", "2026-05-26"], cfg) == ["2026-05-26"]


def test_filter_by_window_any_date_ignores_window():
    cfg = SimpleNamespace(appointment={
        "earliest_date": "2026-06-01",
        "latest_date": "2026-06-30",
        "any_date": True,
    })
    assert _filter_by_window(["2026-05-25"], cfg) == ["2026-05-25"]


def test_filter_by_window_latest_preference_sorts_iso_dates():
    cfg = SimpleNamespace(appointment={"date_preference": "latest"})
    assert _filter_by_window(["2026-05-25", "unknown", "2026-06-02"], cfg) == [
        "2026-06-02",
        "2026-05-25",
        "unknown",
    ]


def test_filter_by_window_random_preference_shuffles():
    cfg = SimpleNamespace(appointment={"date_preference": "random"})

    def reverse(values):
        values.reverse()

    with mock.patch("bot.monitor.random.shuffle", side_effect=reverse) as shuffle:
        assert _filter_by_window(["2026-05-25", "2026-06-02"], cfg) == [
            "2026-06-02",
            "2026-05-25",
        ]
    shuffle.assert_called_once()


def test_clear_appointment_turnstile_uses_solver_without_gui_click():
    cfg = SimpleNamespace(captcha_enabled=True, captcha_provider="capsolver")
    sb = mock.Mock()
    sb.uc_gui_click_captcha = mock.Mock(side_effect=AssertionError("must not move mouse"))

    with (
        mock.patch("bot.monitor.solve_cloudflare_clearance", return_value=False),
        mock.patch("bot.monitor._turnstile_present", return_value=True),
        mock.patch("bot.monitor._wait_for_turnstile_auto_clear", return_value=False),
        mock.patch("bot.monitor._solve_with_paid_service", return_value=True) as solve,
    ):
        _clear_appointment_turnstile(sb, cfg)

    solve.assert_called_once_with(sb, cfg)
    sb.uc_gui_click_captcha.assert_not_called()


def test_go_to_booking_page_clicks_start_button_before_dropdowns():
    sb = FakeBookingSB()
    cfg = SimpleNamespace()

    with (
        mock.patch("bot.monitor.install_turnstile_hook"),
        mock.patch("bot.monitor.wait_out_queue", return_value=True),
        mock.patch("bot.monitor.S.START_BOOKING_BTN", ["#start"]),
        mock.patch("bot.monitor.S.SELECT_CENTRE_TRIGGER", ["#centre"]),
        mock.patch("bot.monitor.S.SELECT_CATEGORY_TRIGGER", []),
        mock.patch("bot.monitor.S.SELECT_SUBCATEGORY_TRIGGER", []),
        mock.patch("bot.monitor.S.CALENDAR_ROOT", []),
    ):
        _go_to_booking_page(sb, cfg)

    assert sb.clicked == [("#start", "css selector")]


def test_go_to_booking_page_uses_text_js_fallback():
    sb = FakeBookingSB(js_click=True)
    cfg = SimpleNamespace()

    with (
        mock.patch("bot.monitor.install_turnstile_hook"),
        mock.patch("bot.monitor.wait_out_queue", return_value=True),
        mock.patch("bot.monitor.S.START_BOOKING_BTN", ["#start"]),
        mock.patch("bot.monitor.S.SELECT_CENTRE_TRIGGER", ["#centre"]),
        mock.patch("bot.monitor.S.SELECT_CATEGORY_TRIGGER", []),
        mock.patch("bot.monitor.S.SELECT_SUBCATEGORY_TRIGGER", []),
        mock.patch("bot.monitor.S.CALENDAR_ROOT", []),
    ):
        _go_to_booking_page(sb, cfg)

    assert sb.clicked == [("js", "script")]

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from bot.monitor import _application_detail_slot_dates, _filter_by_window


class FakeSB:
    def __init__(self, source: str):
        self.source = source

    def get_page_source(self) -> str:
        return self.source


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

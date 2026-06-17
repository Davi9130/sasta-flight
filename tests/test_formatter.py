import pytest
from bot.formatter import format_daily_message, format_history_message, _flight_url
from bot.scanner import ScanResult


def test_flight_url_contains_base_url():
    url = _flight_url("ATQ", "BOM", "2026-03-18")
    assert url.startswith("https://www.google.com/travel/flights/search?tfs=")


def test_flight_url_differs_by_date():
    url1 = _flight_url("ATQ", "BOM", "2026-03-18")
    url2 = _flight_url("ATQ", "BOM", "2026-03-20")
    assert url1 != url2


def test_flight_url_differs_by_stops():
    url_any = _flight_url("ATQ", "BOM", "2026-03-18")
    url_direct = _flight_url("ATQ", "BOM", "2026-03-18", max_stops="direct")
    assert url_any != url_direct


def test_flight_url_no_stops_same_as_any():
    url_none = _flight_url("ATQ", "BOM", "2026-03-18")
    url_any = _flight_url("ATQ", "BOM", "2026-03-18", max_stops="any")
    assert url_none == url_any


def test_flight_url_roundtrip_differs_from_oneway():
    url_ow = _flight_url("VIX", "MXP", "2026-03-18")
    url_rt = _flight_url("VIX", "MXP", "2026-03-18", return_date="2026-03-28")
    assert url_ow != url_rt


def test_format_daily_message_roundtrip():
    result = ScanResult(
        from_airport="VIX",
        to_airport="MXP",
        cheapest_price=4500,
        cheapest_travel_date="2026-03-18",
        cheapest_return_date="2026-03-28",
        cheapest_airline="TAP",
        cheapest_departure="08:30 PM",
        cheapest_duration=735,
        cheapest_stops=1,
        top_days=[
            {"date": "2026-03-18", "return_date": "2026-03-28", "price": 4500},
        ],
        avg_price=5100,
        min_price=4500,
        max_price=6200,
        stay_days=10,
    )
    msg = format_daily_message(result)
    assert "VIX ⇄ MXP" in msg
    assert "10-day stay" in msg
    assert "Mar 18" in msg
    assert "Mar 28" in msg
    assert "4,500" in msg


def test_format_history_message_roundtrip():
    history = [
        {
            "scan_date": "2026-03-07",
            "cheapest_travel_date": "2026-03-18",
            "cheapest_return_date": "2026-03-28",
            "cheapest_price": 4500,
        },
    ]
    msg = format_history_message("VIX", "MXP", history, stay_days=10)
    assert "VIX ⇄ MXP" in msg
    assert "Mar 28" in msg


def test_format_daily_message_basic():
    result = ScanResult(
        from_airport="ATQ",
        to_airport="BOM",
        cheapest_price=3200,
        cheapest_travel_date="2026-03-18",
        cheapest_airline="IndiGo",
        cheapest_departure="06:00 AM",
        cheapest_duration=165,
        cheapest_stops=0,
        top_days=[
            {"date": "2026-03-18", "price": 3200},
            {"date": "2026-03-20", "price": 3450},
            {"date": "2026-03-25", "price": 3500},
        ],
        avg_price=5200,
        min_price=3200,
        max_price=8900,
    )
    msg = format_daily_message(result)
    assert "ATQ" in msg
    assert "BOM" in msg
    assert "3,200" in msg
    assert "IndiGo" in msg
    assert "Nonstop" in msg


def test_format_daily_message_with_trend():
    result = ScanResult(
        from_airport="ATQ",
        to_airport="BOM",
        cheapest_price=3200,
        cheapest_travel_date="2026-03-18",
        cheapest_airline="IndiGo",
        cheapest_departure="06:00 AM",
        cheapest_duration=165,
        cheapest_stops=0,
        top_days=[{"date": "2026-03-18", "price": 3200}],
        avg_price=5200,
        min_price=3200,
        max_price=8900,
    )
    msg = format_daily_message(result, prev_cheapest=3500)
    assert "dropped" in msg.lower() or "↓" in msg.lower() or "down" in msg.lower()


def test_format_daily_message_no_details():
    """When flight details are unavailable."""
    result = ScanResult(
        from_airport="ATQ",
        to_airport="BOM",
        cheapest_price=3200,
        cheapest_travel_date="2026-03-18",
        cheapest_airline=None,
        cheapest_departure=None,
        cheapest_duration=None,
        cheapest_stops=None,
        top_days=[{"date": "2026-03-18", "price": 3200}],
        avg_price=5200,
        min_price=3200,
        max_price=8900,
    )
    msg = format_daily_message(result)
    assert "3,200" in msg


def test_format_history_message():
    history = [
        {"scan_date": "2026-03-07", "cheapest_price": 3400},
        {"scan_date": "2026-03-06", "cheapest_price": 3100},
        {"scan_date": "2026-03-05", "cheapest_price": 3600},
        {"scan_date": "2026-03-04", "cheapest_price": 3200},
        {"scan_date": "2026-03-03", "cheapest_price": 3500},
    ]
    msg = format_history_message("ATQ", "BOM", history)
    assert "ATQ" in msg
    assert "BOM" in msg
    assert "3,100" in msg  # lowest should appear
    assert "█" in msg  # bar chart


def test_format_daily_message_with_stops_filter():
    result = ScanResult(
        from_airport="ATQ",
        to_airport="BOM",
        cheapest_price=3200,
        cheapest_travel_date="2026-03-18",
        cheapest_airline="IndiGo",
        cheapest_departure="06:00 AM",
        cheapest_duration=165,
        cheapest_stops=0,
        top_days=[{"date": "2026-03-18", "price": 3200}],
        avg_price=5200,
        min_price=3200,
        max_price=8900,
    )
    msg = format_daily_message(result, stops_label="Direct")
    assert "Filter: Direct" in msg


def test_format_daily_message_no_filter_label_for_any():
    result = ScanResult(
        from_airport="ATQ",
        to_airport="BOM",
        cheapest_price=3200,
        cheapest_travel_date="2026-03-18",
        cheapest_airline=None,
        cheapest_departure=None,
        cheapest_duration=None,
        cheapest_stops=None,
        top_days=[{"date": "2026-03-18", "price": 3200}],
        avg_price=5200,
        min_price=3200,
        max_price=8900,
    )
    msg = format_daily_message(result)
    assert "Filter" not in msg


def test_format_daily_message_contains_book_links():
    result = ScanResult(
        from_airport="ATQ",
        to_airport="BOM",
        cheapest_price=3200,
        cheapest_travel_date="2026-03-18",
        cheapest_airline="IndiGo",
        cheapest_departure="06:00 AM",
        cheapest_duration=165,
        cheapest_stops=0,
        top_days=[
            {"date": "2026-03-18", "price": 3200},
            {"date": "2026-03-20", "price": 3450},
        ],
        avg_price=5200,
        min_price=3200,
        max_price=8900,
    )
    msg = format_daily_message(result, max_stops="direct")
    assert "[Book →]" in msg
    assert "google.com/travel/flights" in msg
    # Each top day should have a link
    assert msg.count("[Book →]") == 2


@pytest.mark.parametrize(
    ("currency", "symbol", "expected"),
    [
        ("BRL", "R$", "R$3,200"),
        ("USD", "$", "$3,200"),
        ("EUR", "€", "€3,200"),
        ("GBP", "£", "£3,200"),
    ],
)
def test_format_price_currency_symbol(monkeypatch, currency, symbol, expected):
    import importlib

    monkeypatch.setenv("CURRENCY", currency)
    import bot.config as config
    import bot.formatter as formatter

    importlib.reload(config)
    importlib.reload(formatter)

    assert formatter._format_price(3200) == expected

from bot.alerts import evaluate_alerts
from bot.formatter import (
    format_alert_only_message,
    format_daily_message,
    format_deferred_message,
    format_guard_status,
    format_history_message,
    format_price_multi,
    format_via_message,
    md,
    route_label,
)
from bot.search.scan import ScanResult


def _result(**overrides) -> ScanResult:
    days = [
        {
            "date": "2026-12-01", "return_date": "2026-12-11", "price": 4500.0, "from_airport": "GIG",
            "to_airport": "MXP", "airline": "Air_Europa", "departure": "08:30 PM", "duration": 735,
            "stops": 1, "fare_type": "roundtrip", "booking_url": "https://www.google.com/travel/flights/search?tfs=A",
        },
        {
            "date": "2026-12-03", "return_date": "2026-12-13", "price": 4700.0, "from_airport": "VIX",
            "to_airport": "MXP", "fare_type": "split", "out_url": "https://o", "in_url": "https://i",
            "outbound_price": 2000.0, "inbound_price": 2700.0,
        },
    ]
    values = dict(
        from_airport="VIX,GIG", to_airport="MXP", from_airports=["VIX", "GIG"], to_airports=["MXP"],
        currency="BRL", provider="fake", fare_type="roundtrip", top_days=days, avg_price=4600,
        min_price=4500, max_price=4700, stay_days=7, stay_days_max=10, requests_used=18, probes_checked=22,
    )
    values.update(overrides)
    return ScanResult(**values)


async def test_alert_rules(db):
    rid = await db.add_route("VIX", "GRU")
    await db.set_route_alert(rid, target_price=1000, alert_drop_pct=5, alert_on_new_low=1)
    route = await db.get_route(rid)
    hits = await evaluate_alerts(db, route, price=900, travel_date="2026-12-01", prev_price=1000, stats={"min": 950})
    assert {h.rule for h in hits} == {"target", "drop", "new_low"}
    assert not await evaluate_alerts(db, route, price=1100, travel_date="2026-12-01", prev_price=1100, stats={"min": 900})

    await db.record_alert_event(rid, "target", 900, hits[0].fingerprint)
    assert await evaluate_alerts(db, route, price=800, travel_date="2026-12-01", prev_price=None, stats=None) == []


def test_daily_message():
    msg = format_daily_message(_result(), prev_cheapest=5000, fx_amounts={"EUR": 720, "USD": 780}, stats={"median": 5300, "min": 4400})
    assert "✈️ VIX,GIG ⇄ MXP | Next" in msg and "7-10-day stay" in msg
    assert "R$4,500 (≈ €720 / $780)" in msg
    assert "Airports: GIG → MXP" in msg
    assert "Air\\_Europa" in msg
    assert "[Book →](https://www.google.com/travel/flights/search?tfs=A)" in msg
    assert "[Out →](https://o) · [Return →](https://i)" in msg
    assert "dropped 10%" in msg
    assert "18 Google requests" in msg


def test_partial_and_alert_only_messages():
    assert "Partial scan" in format_daily_message(_result(partial=True, partial_reason="captcha"))
    msg = format_alert_only_message(_result(), ["🎯 Target hit"])
    assert msg.startswith("🚨 Deal alert: VIX,GIG ⇄ MXP") and "[Book →]" in msg


def test_deferred_and_status_messages():
    msg = format_deferred_message("VIX → GRU", "captcha", 1800)
    assert "captcha" in msg and "~30 min" in msg
    assert "budget" in format_deferred_message("VIX → GRU", "budget_hour", 90)
    status = format_guard_status(
        {"requests_hour": 3, "per_hour": 60, "requests_day": 9, "per_day": 400,
         "circuit_reason": "http_429", "circuit_remaining": 600, "proxies": []}
    )
    assert "3/60" in status and "HTTP 429" in status and "direct" in status


def test_labels_and_history():
    assert route_label({"from_airport": "VIX", "to_airport": "MXP", "stay_min": 7, "stay_max": 10}) == "VIX ⇄ MXP (7-10d)"
    assert "via SP/RJ" in route_label({"from_airport": "VIX", "to_airport": "MXP", "stay_min": 10, "via_hub": 1, "hub_nights_min": 0, "hub_nights_max": 2})
    assert format_price_multi(100, "BRL", None) == "R$100"
    assert md("a_b*c") == "a\\_b\\*c"
    route = {"from_airport": "VIX", "to_airport": "GRU"}
    assert "No history yet" in format_history_message(route, [])
    history = [
        {"scan_date": "2026-11-03", "cheapest_price": 900, "cheapest_travel_date": "2026-12-01", "currency": "BRL"},
        {"scan_date": "2026-11-02", "cheapest_price": 1000, "cheapest_travel_date": "2026-12-01", "currency": "BRL"},
    ]
    msg = format_history_message(route, history, stats={"min": 900, "median": 950, "latest": 900})
    assert "← lowest" in msg and "Down 10%" in msg


def test_via_message():
    legs = [
        {"date": "2026-11-30", "from_airport": "VIX", "to_airport": "GRU", "price": 250, "airline": "GOL"},
        {"date": "2026-12-01", "from_airport": "GRU", "to_airport": "MXP", "price": 2000},
        {"date": "2026-12-11", "from_airport": "MXP", "to_airport": "GRU", "price": 2000},
        {"date": "2026-12-12", "from_airport": "GRU", "to_airport": "VIX", "price": 250},
    ]
    combo = {"date": "2026-11-30", "return_date": "2026-12-12", "price": 4500, "hub_out": "SAO", "hub_back": "SAO",
             "nights_out": 1, "nights_back": 1, "legs": legs, "from_airport": "VIX", "to_airport": "MXP"}
    result = _result(fare_type="via_hub", top_days=[combo], via_combos=[combo], stay_days=10, stay_days_max=None,
                     hub_nights=0, hub_nights_max=1, direct_price=6000)
    msg = format_via_message(result)
    assert "Ida via São Paulo (1 noite)" in msg
    assert "Direto: R$6,000 (−25%)" in msg
    assert msg.count("[Book →]") == 4

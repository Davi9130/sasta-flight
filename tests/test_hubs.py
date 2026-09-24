from datetime import datetime, timedelta

import pytest

from bot.hubs import (
    HubCombo,
    LegQuote,
    airports_compatible,
    connection_ok,
    join_hub_itineraries,
    parse_via_args,
)


def _q(date, price, origin, dest) -> LegQuote:
    return LegQuote(date=date, price=price, from_airport=origin, to_airport=dest)


def test_same_day_requires_same_longhaul_airport():
    assert airports_compatible("GRU", "GRU", 0) is True
    assert airports_compatible("CGH", "GRU", 0) is False
    assert airports_compatible("CGH", "CGH", 0) is False
    assert airports_compatible("CGH", "GRU", 1) is True
    assert airports_compatible("GIG", "SDU", 1) is True
    assert airports_compatible("GRU", "GIG", 2) is False


def test_connection_gap():
    arrive = datetime(2026, 4, 1, 10, 0)
    assert connection_ok(arrive, arrive + timedelta(hours=4), 0) is True
    assert connection_ok(arrive, arrive + timedelta(hours=3, minutes=59), 0) is False
    assert connection_ok(None, None, 0) is False
    assert connection_ok(None, None, 1) is True


def test_join_picks_cheaper_overnight_and_different_return_hub():
    combos = join_hub_itineraries(
        pos_out=[_q("2026-04-01", 400, "VIX", "GRU"), _q("2026-04-01", 200, "VIX", "CGH")],
        long_out=[
            _q("2026-04-01", 2000, "GRU", "MXP"),
            _q("2026-04-02", 900, "GRU", "MXP"),
        ],
        long_in=[_q("2026-04-12", 800, "MXP", "GIG")],
        pos_in=[
            _q("2026-04-12", 350, "GIG", "VIX"),
            _q("2026-04-13", 180, "SDU", "VIX"),
        ],
        dest_stay_min=10,
        dest_stay_max=10,
        hub_nights_min=0,
        hub_nights_max=1,
    )
    assert combos
    best = combos[0]
    assert isinstance(best, HubCombo)
    assert best.pos_out.to_airport == "CGH"
    assert best.nights_out == 1
    assert best.long_out.date == "2026-04-02"
    assert best.long_in.to_airport == "GIG"
    assert best.nights_back == 1
    assert best.pos_in.from_airport == "SDU"
    assert best.price == 200 + 900 + 800 + 180
    assert best.dest_stay == 10


def test_join_same_day_skips_domestic_only_airport():
    combos = join_hub_itineraries(
        pos_out=[_q("2026-04-01", 100, "VIX", "CGH")],
        long_out=[_q("2026-04-01", 500, "GRU", "MXP")],
        long_in=[_q("2026-04-08", 500, "MXP", "GRU")],
        pos_in=[_q("2026-04-08", 100, "GRU", "VIX")],
        dest_stay_min=7,
        dest_stay_max=7,
        hub_nights_min=0,
        hub_nights_max=0,
    )
    assert combos == []


def test_parse_via_args():
    request = parse_via_args(["VIX", "MXP", "7-10", "0-2", "save"])
    assert request.origins == ["VIX"]
    assert request.destinations == ["MXP"]
    assert (request.stay_min, request.stay_max) == (7, 10)
    assert (request.hub_nights_min, request.hub_nights_max) == (0, 2)
    assert request.save is True

    bare = parse_via_args(["vix,gig", "mxp,bgy", "10"])
    assert bare.origins == ["VIX", "GIG"]
    assert bare.destinations == ["MXP", "BGY"]
    assert bare.hub_nights_min == 0
    assert bare.hub_nights_max == 0
    assert bare.save is False


@pytest.mark.asyncio
async def test_scan_via_confirms_prices_and_drops_tight_connection(monkeypatch):
    from bot.hubs import LegQuote
    from bot.scanner import scan_via_route

    quotes = {
        "out": [LegQuote("2026-04-01", 100, "VIX", "GRU")],
        "long_out": [LegQuote("2026-04-01", 1000, "GRU", "MXP")],
        "long_in": [LegQuote("2026-04-11", 900, "MXP", "GRU")],
        "home": [LegQuote("2026-04-11", 120, "GRU", "VIX")],
    }

    async def fake_calendar(provider, pairs, start, end, currency, country):
        sample = pairs[0]
        if sample[0] == "VIX":
            return quotes["out"]
        if sample[1] == "MXP":
            return quotes["long_out"]
        if sample[0] == "MXP":
            return quotes["long_in"]
        return quotes["home"]

    async def fake_details(origin, dest, travel_date, **kwargs):
        arrive = datetime(2026, 4, 1, 18, 0) if origin == "VIX" else datetime(2026, 4, 11, 18, 0)
        depart = datetime(2026, 4, 1, 20, 0) if dest == "MXP" else datetime(2026, 4, 11, 23, 0)
        return {
            "price": 50,
            "airline": "TEST",
            "departure": "08:00 PM",
            "departure_at": depart if dest in {"MXP", "VIX"} else arrive,
            "arrival_at": arrive if origin in {"VIX", "MXP"} else depart,
            "duration": 90,
            "stops": 0,
        }

    monkeypatch.setattr("bot.scanner._calendar_quotes", fake_calendar)
    monkeypatch.setattr("bot.scanner.scan_flight_details", fake_details)
    monkeypatch.setattr("bot.scanner.scan_route", _async_none)

    result = await scan_via_route("VIX", "MXP", stay_days=10, hub_nights=0, hub_nights_max=0)
    assert result == "NO_MATCHES"


async def _async_none(*args, **kwargs):
    return None


@pytest.mark.asyncio
async def test_scan_via_keeps_legal_connection(monkeypatch):
    from bot.hubs import LegQuote
    from bot.scanner import ScanResult, scan_via_route

    async def fake_calendar(provider, pairs, start, end, currency, country):
        sample = pairs[0]
        if sample == ("VIX", "GRU") or (sample[0] == "VIX" and sample[1] == "GRU"):
            return [LegQuote("2026-04-01", 100, "VIX", "GRU")]
        if sample[1] == "MXP":
            return [LegQuote("2026-04-01", 1000, "GRU", "MXP")]
        if sample[0] == "MXP":
            return [LegQuote("2026-04-11", 900, "MXP", "GRU")]
        if sample[0] == "GRU" and sample[1] == "VIX":
            return [LegQuote("2026-04-11", 120, "GRU", "VIX")]
        return []

    async def fake_details(origin, dest, travel_date, **kwargs):
        if origin == "VIX":
            arrival, departure = datetime(2026, 4, 1, 8, 0), datetime(2026, 4, 1, 6, 0)
        elif dest == "MXP":
            arrival, departure = datetime(2026, 4, 1, 22, 0), datetime(2026, 4, 1, 14, 0)
        elif origin == "MXP":
            arrival, departure = datetime(2026, 4, 11, 8, 0), datetime(2026, 4, 11, 6, 0)
        else:
            arrival, departure = datetime(2026, 4, 11, 20, 0), datetime(2026, 4, 11, 14, 0)
        return {
            "price": 10,
            "airline": "TEST",
            "departure": "02:00 PM",
            "departure_at": departure,
            "arrival_at": arrival,
            "duration": 60,
            "stops": 0,
        }

    async def fake_direct(*args, **kwargs):
        return ScanResult(
            from_airport="VIX",
            to_airport="MXP",
            cheapest_price=9999,
            cheapest_travel_date="2026-04-01",
            cheapest_airline="DIR",
            cheapest_departure=None,
            cheapest_duration=None,
            cheapest_stops=None,
            top_days=[],
            avg_price=9999,
            min_price=9999,
            max_price=9999,
        )

    monkeypatch.setattr("bot.scanner._calendar_quotes", fake_calendar)
    monkeypatch.setattr("bot.scanner.scan_flight_details", fake_details)
    monkeypatch.setattr("bot.scanner.scan_route", fake_direct)

    result = await scan_via_route("VIX", "MXP", stay_days=10, hub_nights=0, hub_nights_max=0)
    assert isinstance(result, ScanResult)
    assert result.fare_type == "via_hub"
    assert result.cheapest_price == 40
    assert result.direct_price == 9999
    assert len(result.via_combos[0]["legs"]) == 4


def test_parse_via_args_rejects_bad_input():
    with pytest.raises(ValueError, match="usage"):
        parse_via_args(["VIX"])
    with pytest.raises(ValueError, match="hub"):
        parse_via_args(["VIX", "MXP", "10", "0-20"])
    with pytest.raises(ValueError, match="stay"):
        parse_via_args(["VIX", "MXP", "0"])
    with pytest.raises(ValueError, match="airports"):
        parse_via_args(["VI", "MXP", "10"])

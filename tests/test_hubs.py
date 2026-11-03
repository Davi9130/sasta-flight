from datetime import date, datetime

import pytest

from bot.search.hubs import (
    LegQuote,
    city_of,
    connection_ok,
    join_hub_itineraries,
    parse_via_args,
    scan_via_route,
)
from bot.search.scan import NO_MATCHES
from tests.conftest import FakeFares


def test_parse_via_args():
    req = parse_via_args(["VIX", "MXP", "7-10", "0-2", "save"])
    assert (req.origins, req.destinations, req.stay_min, req.stay_max) == (["VIX"], ["MXP"], 7, 10)
    assert (req.hub_nights_min, req.hub_nights_max, req.save) == (0, 2, True)
    assert parse_via_args(["VIX", "MXP", "10"]).hub_nights_max == 0
    for bad, code in ((["VIX"], "usage"), (["VI", "MXP", "7"], "airports"), (["VIX", "MXP", "0"], "stay"), (["VIX", "MXP", "7", "0-20"], "hub")):
        with pytest.raises(ValueError, match=code):
            parse_via_args(bad)


def test_city_and_connection():
    assert city_of("cgh") == "SAO" and city_of("SDU") == "RIO" and city_of("VIX") is None
    arrive = datetime(2026, 12, 1, 10, 0)
    assert connection_ok(arrive, datetime(2026, 12, 1, 15, 0), 0)
    assert not connection_ok(arrive, datetime(2026, 12, 1, 12, 0), 0)
    assert connection_ok(None, None, 1)
    assert not connection_ok(None, None, 0)


def _leg(d, f, t, price, dep=None, arr=None):
    return LegQuote(d, price, f, t, dep, arr)


def test_join_rejects_tight_same_day_transfer():
    pos_out = [_leg("2026-12-01", "VIX", "GRU", 200, arr=datetime(2026, 12, 1, 10))]
    long_tight = [_leg("2026-12-01", "GRU", "MXP", 2000, dep=datetime(2026, 12, 1, 11))]
    long_ok = [_leg("2026-12-01", "GRU", "MXP", 2100, dep=datetime(2026, 12, 1, 18))]
    long_in = [_leg("2026-12-11", "MXP", "GRU", 2000, arr=datetime(2026, 12, 12, 6))]
    pos_in = [_leg("2026-12-12", "GRU", "VIX", 200, dep=datetime(2026, 12, 12, 13))]
    kwargs = dict(dest_stay_min=10, dest_stay_max=10, hub_nights_min=0, hub_nights_max=1)
    assert join_hub_itineraries(pos_out, long_tight, long_in, pos_in, **kwargs) == []
    combos = join_hub_itineraries(pos_out, long_ok, long_in, pos_in, **kwargs)
    assert len(combos) == 1
    assert combos[0].price == 4500 and combos[0].nights_out == 0 and combos[0].nights_back == 1


def _prices(f, t, d, r):
    if r:
        return 6000  # direct round trip
    if {f, t} & {"VIX"}:
        return 250
    return 2000


async def test_scan_via_route_builds_combos_and_compares_direct():
    fares = FakeFares(_prices)
    result = await scan_via_route(
        fares, origins=["VIX"], destinations=["MXP"], stay_min=10, hub_nights_min=0, hub_nights_max=1,
        currency="BRL", today=date(2026, 11, 3), budget_limit=40, hubs=["GRU"],
    )
    assert result is not NO_MATCHES
    best = result.via_combos[0]
    assert best["price"] == 4500
    assert len(best["legs"]) == 4
    # FakeFares flights all depart 10:00 / arrive 12:00, so same-day transfers fail.
    assert best["nights_out"] == 1 and best["nights_back"] == 1
    assert result.direct_price == 6000
    assert result.requests_used <= 40


async def test_scan_via_route_no_matches():
    fares = FakeFares(lambda *a: None)
    result = await scan_via_route(
        fares, origins=["VIX"], destinations=["MXP"], stay_min=10, currency="BRL", today=date(2026, 11, 3), hubs=["GRU"],
    )
    assert result is NO_MATCHES

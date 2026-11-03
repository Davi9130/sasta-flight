from datetime import date, datetime

import pytest

from bot.search.planner import coarse_probes, neighbor_probes, sample_stays, stay_length, window_dates
from bot.search.scan import (
    NO_MATCHES,
    ScanDeferred,
    ScanFailed,
    airport_pairs,
    parse_airport_list,
    parse_stay_range,
    scan_route,
)
from tests.conftest import FakeFares

TODAY = date(2026, 11, 3)
DATES = window_dates(date(2026, 11, 4), 30)


def _offset(d: str) -> int:
    return (datetime.strptime(d, "%Y-%m-%d").date() - date(2026, 11, 4)).days


def test_parsers():
    assert parse_airport_list("vix, gig,VIX") == ["VIX", "GIG"]
    assert parse_stay_range("7-10") == (7, 10)
    assert parse_stay_range("10-7") == (7, 10)
    assert parse_stay_range("5") == (5, 5)
    assert parse_stay_range(None) == (None, None)
    with pytest.raises(ValueError):
        parse_stay_range("abc")


def test_sample_stays():
    assert sample_stays(7, 10) == [7, 8, 10]
    assert sample_stays(10, None) == [10]


def test_coarse_grid_fits_budget_and_covers_every_combo():
    pairs = [("VIX", "MXP"), ("GIG", "MXP")]
    probes = coarse_probes(pairs, DATES, [7, 8, 10], budget=15, base_step=3)
    assert 0 < len(probes) <= 15
    combos = {(p.from_airport, p.stay) for p in probes}
    assert combos == {(f, s) for f, _ in pairs for s in (7, 8, 10)}


def test_coarse_grid_when_combos_exceed_budget():
    pairs = [(f"A{i}A", "MXP") for i in range(5)]
    probes = coarse_probes(pairs, DATES, [7, 10], budget=4, base_step=3)
    assert len(probes) == 4


def test_neighbor_probes_respect_window_and_stay():
    probe = coarse_probes([("VIX", "MXP")], DATES[:1], [7], budget=1, base_step=1)[0]
    near = neighbor_probes(probe, distance=1, window=set(DATES), stay_min=7, stay_max=8)
    assert all(p.date in DATES for p in near)
    assert all(7 <= p.stay <= 8 for p in near)
    assert any(p.stay == 8 for p in near)


def test_airport_pairs_unknown_first_then_cheapest():
    priority = {("VIX", "MXP"): 5000.0, ("GIG", "MXP"): 3000.0}
    pairs = airport_pairs(["VIX", "GIG", "CNF"], ["MXP"], priority)
    assert pairs[0] == ("CNF", "MXP")
    assert pairs[1:] == [("GIG", "MXP"), ("VIX", "MXP")]


async def test_oneway_refine_finds_off_grid_minimum():
    # V-shaped prices with the bottom on a day the coarse grid skips.
    fares = FakeFares(lambda f, t, d, r: 1000 + 50 * abs(_offset(d) - 8))
    result = await scan_route(fares, origins=["VIX"], destinations=["GRU"], currency="BRL", today=TODAY, budget_limit=20)
    assert result.cheapest_travel_date == DATES[8]
    assert result.cheapest_price == 1000
    assert result.fare_type == "oneway"
    assert result.requests_used <= 20 and len(fares.calls) == result.requests_used
    assert len(result.top_days) <= 5
    assert result.top_days[0]["booking_url"]


async def test_roundtrip_flexible_stay_prefers_cheapest_stay():
    def price(f, t, d, r):
        return 3000 + 100 * abs(stay_length(d, r) - 9) + 10 * _offset(d)

    fares = FakeFares(price)
    result = await scan_route(
        fares, origins=["VIX"], destinations=["MXP"], stay_min=7, stay_max=10, currency="BRL", today=TODAY, budget_limit=30
    )
    best = result.top_days[0]
    assert best["stay_days"] == 9
    assert all(7 <= d["stay_days"] <= 10 for d in result.top_days)
    assert result.fare_type == "roundtrip"


async def test_block_before_any_quote_defers():
    fares = FakeFares(lambda *a: 100, block_after=0)
    with pytest.raises(ScanDeferred) as exc:
        await scan_route(fares, origins=["VIX"], destinations=["GRU"], currency="BRL", today=TODAY)
    assert exc.value.reason == "http_429"


async def test_block_mid_scan_returns_partial():
    fares = FakeFares(lambda f, t, d, r: 100 + _offset(d), block_after=3)
    result = await scan_route(fares, origins=["VIX"], destinations=["GRU"], currency="BRL", today=TODAY)
    assert result.partial and result.partial_reason == "http_429"
    assert len(fares.calls) == 3


async def test_no_flights_and_all_failures():
    fares = FakeFares(lambda *a: None)
    assert await scan_route(fares, origins=["VIX"], destinations=["GRU"], currency="BRL", today=TODAY) is NO_MATCHES

    from bot.fares.models import SearchFailed

    class Failing(FakeFares):
        async def search(self, *a, **k):
            raise SearchFailed("timeout")

    with pytest.raises(ScanFailed):
        await scan_route(Failing(lambda *a: 1), origins=["VIX"], destinations=["GRU"], currency="BRL", today=TODAY)


async def test_split_tickets_replace_pricier_round_trip():
    def price(f, t, d, r):
        return 2000 if r else 600

    fares = FakeFares(price)
    result = await scan_route(
        fares, origins=["VIX"], destinations=["MXP"], stay_min=10, currency="BRL",
        today=TODAY, budget_limit=20, split_tickets=True,
    )
    best = result.top_days[0]
    assert best["fare_type"] == "split"
    assert best["price"] == 1200
    assert best["out_url"] and best["in_url"]
    assert result.requests_used <= 20

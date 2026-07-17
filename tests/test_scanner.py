import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta

from bot.scanner import (
    DayDeal,
    ScanResult,
    clear_search_cache,
    parse_airport_list,
    parse_stay_range,
    scan_flight_details,
    scan_route,
    scan_route_dates,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_search_cache()
    yield
    clear_search_cache()


@pytest.fixture
def future_outbound_dates():
    base = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=3)
    return [base + timedelta(days=i) for i in range(7)]


@pytest.fixture
def mock_date_results(future_outbound_dates):
    results = []
    for date, price in zip(
        future_outbound_dates,
        [5000, 3200, 4500, 3800, 6000, 3500, 4200],
    ):
        mock = MagicMock()
        mock.date = [date]
        mock.price = price
        results.append(mock)
    return results


@pytest.fixture
def cheapest_outbound_date(future_outbound_dates):
    return future_outbound_dates[1]


@pytest.fixture
def mock_flight_results(cheapest_outbound_date):
    leg = MagicMock()
    leg.airline.value = "IndiGo"
    leg.departure_datetime = cheapest_outbound_date.replace(hour=6, minute=0)
    leg.arrival_datetime = cheapest_outbound_date.replace(hour=8, minute=45)
    leg.departure_airport.value = "ATQ"
    leg.arrival_airport.value = "BOM"

    flight = MagicMock()
    flight.price = 3200
    flight.duration = 165
    flight.stops = 0
    flight.legs = [leg]
    return [flight]


@pytest.fixture
def mock_provider(mock_date_results, mock_flight_results):
    provider = MagicMock()
    provider.name = "mock"
    provider.search_dates.return_value = mock_date_results
    provider.search_flights.return_value = mock_flight_results
    return provider


def test_parse_airport_list():
    assert parse_airport_list("VIX,GIG") == ["VIX", "GIG"]
    assert parse_airport_list("vix") == ["VIX"]


def test_parse_stay_range():
    assert parse_stay_range(10) == (10, 10)
    assert parse_stay_range("7-10") == (7, 10)
    assert parse_stay_range("10-7") == (7, 10)


@pytest.mark.asyncio
async def test_scan_route_dates_roundtrip(mock_date_results, cheapest_outbound_date, mock_provider):
    for mock in mock_date_results:
        outbound = mock.date[0]
        mock.date = (outbound, outbound + timedelta(days=10))

    mock_provider.search_dates.return_value = mock_date_results
    result = await scan_route_dates("VIX", "MXP", days=7, stay_days=10, provider=mock_provider)

    assert len(result) == 7
    assert result[0]["return_date"] == (cheapest_outbound_date + timedelta(days=10)).strftime("%Y-%m-%d")
    assert "price" in result[0]
    call_args = mock_provider.search_dates.call_args[0][0]
    from fli.models import TripType

    assert call_args.trip_type == TripType.ROUND_TRIP
    assert call_args.duration == 10


@pytest.mark.asyncio
async def test_scan_flight_details_roundtrip(mock_flight_results, cheapest_outbound_date, mock_provider):
    outbound = cheapest_outbound_date.strftime("%Y-%m-%d")
    return_date = (cheapest_outbound_date + timedelta(days=10)).strftime("%Y-%m-%d")
    rt_result = (mock_flight_results[0], mock_flight_results[0])
    mock_provider.search_flights.return_value = [rt_result]
    result = await scan_flight_details(
        "VIX", "MXP", outbound, return_date=return_date, max_stops="direct", provider=mock_provider
    )
    assert result["price"] == 3200


@pytest.mark.asyncio
async def test_scan_route_roundtrip(mock_date_results, mock_flight_results, cheapest_outbound_date, mock_provider):
    for mock in mock_date_results:
        outbound = mock.date[0]
        mock.date = (outbound, outbound + timedelta(days=10))
    mock_provider.search_dates.return_value = mock_date_results
    rt_result = (mock_flight_results[0], mock_flight_results[0])
    mock_provider.search_flights.return_value = [rt_result]
    expected_return = (cheapest_outbound_date + timedelta(days=10)).strftime("%Y-%m-%d")

    result = await scan_route("VIX", "MXP", stay_days=10, provider=mock_provider)

    assert result is not None
    assert result.stay_days == 10
    assert result.cheapest_return_date == expected_return
    assert len(result.top_days) == 5
    assert result.top_days[0]["return_date"] == expected_return


@pytest.mark.asyncio
async def test_scan_route_dates_fallback_generates_dates():
    tomorrow = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    with patch("bot.scanner._scan_roundtrip_dates", return_value=[]), patch(
        "bot.scanner._scan_oneway_dates", return_value=[]
    ):
        result = await scan_route_dates("VIX", "MXP", days=2, stay_days=10)

    assert len(result) == 3
    assert result[0]["return_date"] == (tomorrow + timedelta(days=10)).strftime("%Y-%m-%d")
    assert "price" not in result[0]


@pytest.mark.asyncio
async def test_scan_route_dates(mock_provider, cheapest_outbound_date):
    result = await scan_route_dates("ATQ", "BOM", days=7, provider=mock_provider)
    assert len(result) == 7
    assert result[0]["price"] == 3200
    assert result[0]["date"] == cheapest_outbound_date.strftime("%Y-%m-%d")


@pytest.mark.asyncio
async def test_scan_flight_details(mock_provider, cheapest_outbound_date):
    travel_date = cheapest_outbound_date.strftime("%Y-%m-%d")
    result = await scan_flight_details("ATQ", "BOM", travel_date, provider=mock_provider)
    assert result["price"] == 3200
    assert result["airline"] == "IndiGo"


@pytest.mark.asyncio
async def test_scan_route_dates_empty():
    provider = MagicMock()
    provider.name = "mock"
    provider.search_dates.return_value = []
    result = await scan_route_dates("ATQ", "BOM", days=7, provider=provider)
    assert len(result) == 8
    assert "price" not in result[0]


@pytest.mark.asyncio
async def test_scan_route_full(mock_provider):
    result = await scan_route("ATQ", "BOM", provider=mock_provider)
    assert result is not None
    assert result.cheapest_price == 3200
    assert result.cheapest_airline == "IndiGo"
    assert len(result.top_days) == 5
    assert result.currency


@pytest.mark.asyncio
async def test_scan_route_reranks_by_confirmed_price(future_outbound_dates):
    """Calendar order must not win when detailed prices diverge."""
    calendar = []
    for i, date in enumerate(future_outbound_dates):
        mock = MagicMock()
        mock.date = [date]
        mock.price = 1000 + i  # calendar says earliest is cheapest
        calendar.append(mock)

    provider = MagicMock()
    provider.name = "mock"
    provider.search_dates.return_value = calendar

    def flight_at(price, date):
        leg = MagicMock()
        leg.airline.value = "X"
        leg.departure_datetime = date.replace(hour=6, minute=0)
        flight = MagicMock()
        flight.price = price
        flight.duration = 100
        flight.stops = 0
        flight.legs = [leg]
        return [flight]

    # Detail prices: first calendar day expensive, later day cheap
    detail_prices = [9000, 8000, 1000, 7000, 6000, 5000, 4000]
    provider.search_flights.side_effect = [
        flight_at(p, d) for p, d in zip(detail_prices, future_outbound_dates)
    ]

    result = await scan_route("ATQ", "BOM", provider=provider, candidate_pool=7)
    assert isinstance(result, ScanResult)
    assert result.cheapest_price == 1000
    assert result.cheapest_travel_date == future_outbound_dates[2].strftime("%Y-%m-%d")
    assert result.top_days[0]["price"] == 1000


@pytest.mark.asyncio
async def test_scan_flight_details_with_max_stops(mock_provider, cheapest_outbound_date):
    from fli.models import MaxStops

    travel_date = cheapest_outbound_date.strftime("%Y-%m-%d")
    await scan_flight_details("ATQ", "BOM", travel_date, max_stops="direct", provider=mock_provider)
    call_args = mock_provider.search_flights.call_args[0][0]
    assert call_args.stops == MaxStops.NON_STOP


@pytest.mark.asyncio
async def test_scan_route_skips_dates_without_matching_flights(mock_date_results, future_outbound_dates):
    provider = MagicMock()
    provider.name = "mock"
    provider.search_dates.return_value = mock_date_results

    leg = MagicMock()
    leg.airline.value = "IndiGo"
    leg.departure_datetime = future_outbound_dates[2].replace(hour=6, minute=0)
    valid_flight = MagicMock()
    valid_flight.price = 4500
    valid_flight.duration = 165
    valid_flight.stops = 0
    valid_flight.legs = [leg]
    provider.search_flights.side_effect = [
        [],
        [],
        [valid_flight],
        [valid_flight],
        [valid_flight],
        [valid_flight],
        [valid_flight],
    ]

    result = await scan_route("ATQ", "BOM", max_stops="direct", provider=provider)
    assert result is not None
    assert len(result.top_days) == 5


@pytest.mark.asyncio
async def test_scan_route_multi_airport_pool(future_outbound_dates):
    provider = MagicMock()
    provider.name = "mock"

    def dates_for(price_base):
        out = []
        for i, date in enumerate(future_outbound_dates[:3]):
            m = MagicMock()
            m.date = [date]
            m.price = price_base + i
            out.append(m)
        return out

    # First pair expensive, second pair cheaper
    provider.search_dates.side_effect = [dates_for(5000), dates_for(2000)]

    def mk(price):
        leg = MagicMock()
        leg.airline.value = "A"
        leg.departure_datetime = future_outbound_dates[0].replace(hour=8, minute=0)
        f = MagicMock()
        f.price = price
        f.duration = 120
        f.stops = 0
        f.legs = [leg]
        return [f]

    provider.search_flights.side_effect = lambda *a, **k: mk(2100)

    result = await scan_route(["VIX", "GIG"], "MXP", provider=provider, candidate_pool=6)
    assert result is not None
    assert "VIX" in result.from_airport
    assert "GIG" in result.from_airport


@pytest.mark.asyncio
async def test_split_ticket_beats_roundtrip(future_outbound_dates):
    outbound = future_outbound_dates[0]
    ret = outbound + timedelta(days=10)
    cal = MagicMock()
    cal.date = (outbound, ret)
    cal.price = 5000

    provider = MagicMock()
    provider.name = "mock"
    provider.search_dates.return_value = [cal]

    def mk(price):
        leg = MagicMock()
        leg.airline.value = "A"
        leg.departure_datetime = outbound.replace(hour=8, minute=0)
        f = MagicMock()
        f.price = price
        f.duration = 200
        f.stops = 1
        f.legs = [leg]
        return [f]

    # First call: round-trip package; then outbound OW; then inbound OW
    provider.search_flights.side_effect = [
        [(mk(5000)[0], mk(5000)[0])],
        mk(1800),
        mk(1900),
    ]

    with patch("bot.scanner.ENABLE_SPLIT_TICKETS", True):
        result = await scan_route("VIX", "MXP", stay_days=10, provider=provider, candidate_pool=1)

    assert result is not None
    assert result.fare_type == "split"
    assert result.cheapest_price == 3700


@pytest.mark.asyncio
async def test_roundtrip_rejects_wrong_stay_length(future_outbound_dates):
    outbound = future_outbound_dates[0]
    # Calendar returns wrong stay (15 days instead of 10)
    cal = MagicMock()
    cal.date = (outbound, outbound + timedelta(days=15))
    cal.price = 3000
    provider = MagicMock()
    provider.name = "mock"
    provider.search_dates.return_value = [cal]

    from bot.scanner import _scan_roundtrip_dates

    tomorrow = datetime.now() + timedelta(days=1)
    end = tomorrow + timedelta(days=7)
    result = await _scan_roundtrip_dates(
        provider, "VIX", "MXP", 10, tomorrow, end, "BRL", "BR"
    )
    assert result == []

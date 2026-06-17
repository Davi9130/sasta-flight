import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta
from bot.scanner import scan_route_dates, scan_flight_details, ScanResult


@pytest.fixture
def future_outbound_dates():
    base = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=3)
    return [base + timedelta(days=i) for i in range(7)]


@pytest.fixture
def mock_date_results(future_outbound_dates):
    """Simulate fli SearchDates response."""
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
    """Simulate fli SearchFlights response."""
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


@pytest.mark.asyncio
async def test_scan_route_dates_roundtrip(mock_date_results, cheapest_outbound_date):
    for mock in mock_date_results:
        outbound = mock.date[0]
        mock.date = (outbound, outbound + timedelta(days=10))

    with patch("bot.scanner.SearchDates") as MockSearch:
        MockSearch.return_value.search.return_value = mock_date_results
        result = await scan_route_dates("VIX", "MXP", days=7, stay_days=10)

    assert len(result) == 7
    assert result[0]["return_date"] == (cheapest_outbound_date + timedelta(days=10)).strftime("%Y-%m-%d")
    assert "price" in result[0]

    call_args = MockSearch.return_value.search.call_args[0][0]
    from fli.models import TripType
    assert call_args.trip_type == TripType.ROUND_TRIP
    assert len(call_args.flight_segments) == 2


@pytest.mark.asyncio
async def test_scan_flight_details_roundtrip(mock_flight_results, cheapest_outbound_date):
    outbound = cheapest_outbound_date.strftime("%Y-%m-%d")
    return_date = (cheapest_outbound_date + timedelta(days=10)).strftime("%Y-%m-%d")
    rt_result = (mock_flight_results[0], mock_flight_results[0])
    with patch("bot.scanner.SearchFlights") as MockSearch:
        MockSearch.return_value.search.return_value = [rt_result]
        result = await scan_flight_details(
            "VIX", "MXP", outbound, return_date=return_date, max_stops="direct"
        )

    assert result["price"] == 3200
    call_args = MockSearch.return_value.search.call_args[0][0]
    from fli.models import TripType
    assert call_args.trip_type == TripType.ROUND_TRIP
    assert len(call_args.flight_segments) == 2


@pytest.mark.asyncio
async def test_scan_route_roundtrip(mock_date_results, mock_flight_results, cheapest_outbound_date):
    for mock in mock_date_results:
        outbound = mock.date[0]
        mock.date = (outbound, outbound + timedelta(days=10))

    rt_result = (mock_flight_results[0], mock_flight_results[0])
    expected_return = (cheapest_outbound_date + timedelta(days=10)).strftime("%Y-%m-%d")
    with patch("bot.scanner.SearchDates") as MockDates, \
         patch("bot.scanner.SearchFlights") as MockFlights:
        MockDates.return_value.search.return_value = mock_date_results
        MockFlights.return_value.search.return_value = [rt_result]

        result = await scan_route("VIX", "MXP", stay_days=10)

    assert result is not None
    assert result.stay_days == 10
    assert result.cheapest_return_date == expected_return
    assert len(result.top_days) == 5
    assert result.top_days[0]["return_date"] == expected_return


@pytest.mark.asyncio
async def test_scan_route_dates(mock_date_results, cheapest_outbound_date):
    with patch("bot.scanner.SearchDates") as MockSearch:
        MockSearch.return_value.search.return_value = mock_date_results
        result = await scan_route_dates("ATQ", "BOM", days=7)

    assert len(result) == 7
    assert result[0]["price"] == 3200
    assert result[0]["date"] == cheapest_outbound_date.strftime("%Y-%m-%d")


@pytest.mark.asyncio
async def test_scan_flight_details(mock_flight_results, cheapest_outbound_date):
    travel_date = cheapest_outbound_date.strftime("%Y-%m-%d")
    with patch("bot.scanner.SearchFlights") as MockSearch:
        MockSearch.return_value.search.return_value = mock_flight_results
        result = await scan_flight_details("ATQ", "BOM", travel_date)

    assert result["price"] == 3200
    assert result["airline"] == "IndiGo"
    assert result["duration"] == 165
    assert result["stops"] == 0


@pytest.mark.asyncio
async def test_scan_route_dates_empty():
    with patch("bot.scanner.SearchDates") as MockSearch:
        MockSearch.return_value.search.return_value = []
        result = await scan_route_dates("ATQ", "BOM", days=7)

    assert result == []


@pytest.mark.asyncio
async def test_scan_route_full(mock_date_results, mock_flight_results):
    with patch("bot.scanner.SearchDates") as MockDates, \
         patch("bot.scanner.SearchFlights") as MockFlights:
        MockDates.return_value.search.return_value = mock_date_results
        MockFlights.return_value.search.return_value = mock_flight_results

        result = await scan_route("ATQ", "BOM")

    assert result is not None
    assert result.cheapest_price == 3200
    assert result.cheapest_airline == "IndiGo"
    assert len(result.top_days) == 5


from fli.models import MaxStops


@pytest.mark.asyncio
async def test_scan_flight_details_with_max_stops(mock_flight_results, cheapest_outbound_date):
    travel_date = cheapest_outbound_date.strftime("%Y-%m-%d")
    with patch("bot.scanner.SearchFlights") as MockSearch:
        MockSearch.return_value.search.return_value = mock_flight_results
        result = await scan_flight_details("ATQ", "BOM", travel_date, max_stops="direct")

    call_args = MockSearch.return_value.search.call_args[0][0]
    assert call_args.stops == MaxStops.NON_STOP


@pytest.mark.asyncio
async def test_scan_flight_details_default_max_stops(mock_flight_results, cheapest_outbound_date):
    travel_date = cheapest_outbound_date.strftime("%Y-%m-%d")
    with patch("bot.scanner.SearchFlights") as MockSearch:
        MockSearch.return_value.search.return_value = mock_flight_results
        result = await scan_flight_details("ATQ", "BOM", travel_date)

    call_args = MockSearch.return_value.search.call_args[0][0]
    assert call_args.stops == MaxStops.ANY


@pytest.mark.asyncio
async def test_scan_route_skips_dates_without_matching_flights(
    mock_date_results, future_outbound_dates
):
    """When stops filter causes some dates to have no flights, skip them."""
    with patch("bot.scanner.SearchDates") as MockDates, \
         patch("bot.scanner.SearchFlights") as MockFlights:
        MockDates.return_value.search.return_value = mock_date_results
        leg = MagicMock()
        leg.airline.value = "IndiGo"
        leg.departure_datetime = future_outbound_dates[2].replace(hour=6, minute=0)
        valid_flight = MagicMock()
        valid_flight.price = 4500
        valid_flight.duration = 165
        valid_flight.stops = 0
        valid_flight.legs = [leg]
        MockFlights.return_value.search.side_effect = [
            [],
            [],
            [valid_flight],
            [valid_flight],
            [valid_flight],
            [valid_flight],
            [valid_flight],
        ]

        result = await scan_route("ATQ", "BOM", max_stops="direct")

    assert result is not None
    assert len(result.top_days) == 5


from bot.scanner import scan_route

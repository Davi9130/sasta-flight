import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from fli.models import (
    Airport,
    DateSearchFilters,
    FlightSearchFilters,
    FlightSegment,
    MaxStops,
    PassengerInfo,
    SeatType,
    SortBy,
    TripType,
)
from fli.search import SearchDates, SearchFlights

from bot.config import COUNTRY, CURRENCY, DAYS_TO_SCAN, TOP_CHEAPEST

logger = logging.getLogger(__name__)

# Sentinel: scan found dates but no flights matched the stops filter.
# Distinct from None (which means the scan itself failed).
NO_MATCHES = "NO_MATCHES"

STOPS_MAP = {
    "any": MaxStops.ANY,
    "direct": MaxStops.NON_STOP,
    "1stop": MaxStops.ONE_STOP_OR_FEWER,
    "2stops": MaxStops.TWO_OR_FEWER_STOPS,
}


@dataclass
class ScanResult:
    from_airport: str
    to_airport: str
    cheapest_price: float
    cheapest_travel_date: str
    cheapest_airline: str | None
    cheapest_departure: str | None
    cheapest_duration: int | None
    cheapest_stops: int | None
    top_days: list[dict]
    avg_price: float
    min_price: float
    max_price: float
    stay_days: int | None = None
    cheapest_return_date: str | None = None


def _get_airport(code: str):
    """Try to get Airport enum, fall back to raw string."""
    try:
        return Airport[code.upper()]
    except KeyError:
        return code.upper()


def _make_segment(from_code: str, to_code: str, travel_date: str) -> FlightSegment:
    return FlightSegment(
        departure_airport=[[_get_airport(from_code), 0]],
        arrival_airport=[[_get_airport(to_code), 0]],
        travel_date=travel_date,
    )


def _return_date(outbound_date: str, stay_days: int) -> str:
    outbound = datetime.strptime(outbound_date, "%Y-%m-%d")
    return (outbound + timedelta(days=stay_days)).strftime("%Y-%m-%d")


def _parse_rt_search_result(flights) -> tuple | None:
    """Extract outbound flight from a round-trip SearchFlights response."""
    if not flights:
        return None
    first = flights[0]
    if isinstance(first, tuple):
        return first[0]
    return first


def _flight_details_from_result(flight) -> dict:
    leg = flight.legs[0] if flight.legs else None
    return {
        "price": flight.price,
        "airline": leg.airline.value if leg else None,
        "departure": leg.departure_datetime.strftime("%I:%M %p") if leg else None,
        "duration": flight.duration,
        "stops": flight.stops,
    }


async def _scan_oneway_dates(from_code: str, to_code: str, start: datetime, end: datetime) -> list[dict]:
    filters = DateSearchFilters(
        passenger_info=PassengerInfo(adults=1),
        flight_segments=[_make_segment(from_code, to_code, start.strftime("%Y-%m-%d"))],
        from_date=start.strftime("%Y-%m-%d"),
        to_date=end.strftime("%Y-%m-%d"),
    )
    search = SearchDates()
    results = await asyncio.to_thread(
        search.search, filters, currency=CURRENCY, country=COUNTRY
    )
    if not results:
        return []

    date_prices = []
    for r in results:
        date_prices.append({
            "date": r.date[0].strftime("%Y-%m-%d"),
            "price": r.price,
        })
    return sorted(date_prices, key=lambda x: x["price"])


async def _scan_roundtrip_dates(
    from_code: str, to_code: str, stay_days: int, start: datetime, end: datetime
) -> list[dict]:
    return_start = (start + timedelta(days=stay_days)).strftime("%Y-%m-%d")
    filters = DateSearchFilters(
        trip_type=TripType.ROUND_TRIP,
        passenger_info=PassengerInfo(adults=1),
        flight_segments=[
            _make_segment(from_code, to_code, start.strftime("%Y-%m-%d")),
            _make_segment(to_code, from_code, return_start),
        ],
        from_date=start.strftime("%Y-%m-%d"),
        to_date=end.strftime("%Y-%m-%d"),
    )
    search = SearchDates()
    results = await asyncio.to_thread(
        search.search, filters, currency=CURRENCY, country=COUNTRY
    )
    if not results:
        return []

    date_prices = []
    for r in results:
        outbound = r.date[0].strftime("%Y-%m-%d")
        if len(r.date) > 1:
            return_date = r.date[1].strftime("%Y-%m-%d")
        else:
            return_date = _return_date(outbound, stay_days)
        date_prices.append({
            "date": outbound,
            "return_date": return_date,
            "price": r.price,
        })

    expected_stay = timedelta(days=stay_days)
    exact = [
        d for d in date_prices
        if datetime.strptime(d["return_date"], "%Y-%m-%d")
        - datetime.strptime(d["date"], "%Y-%m-%d") == expected_stay
    ]
    ranked = exact if exact else date_prices
    return sorted(ranked, key=lambda x: x["price"])


async def scan_route_dates(
    from_code: str, to_code: str, days: int = DAYS_TO_SCAN, stay_days: int | None = None
) -> list[dict]:
    """Get prices for the next N days. Returns list sorted by price."""
    tomorrow = datetime.now() + timedelta(days=1)
    end_date = tomorrow + timedelta(days=days)

    if stay_days:
        date_prices = await _scan_roundtrip_dates(
            from_code, to_code, stay_days, tomorrow, end_date
        )
        if date_prices:
            return date_prices
        logger.warning(
            "Round-trip calendar empty for %s -> %s (%sd), ranking outbound dates only",
            from_code,
            to_code,
            stay_days,
        )

    return await _scan_oneway_dates(from_code, to_code, tomorrow, end_date)


async def scan_flight_details(
    from_code: str,
    to_code: str,
    travel_date: str,
    max_stops: str = "any",
    return_date: str | None = None,
) -> dict | None:
    """Get flight details for a specific date. Returns cheapest flight info."""
    if return_date:
        filters = FlightSearchFilters(
            trip_type=TripType.ROUND_TRIP,
            passenger_info=PassengerInfo(adults=1),
            flight_segments=[
                _make_segment(from_code, to_code, travel_date),
                _make_segment(to_code, from_code, return_date),
            ],
            seat_type=SeatType.ECONOMY,
            sort_by=SortBy.CHEAPEST,
            stops=STOPS_MAP.get(max_stops, MaxStops.ANY),
        )
    else:
        filters = FlightSearchFilters(
            passenger_info=PassengerInfo(adults=1),
            flight_segments=[_make_segment(from_code, to_code, travel_date)],
            seat_type=SeatType.ECONOMY,
            sort_by=SortBy.CHEAPEST,
            stops=STOPS_MAP.get(max_stops, MaxStops.ANY),
        )

    search = SearchFlights()
    flights = await asyncio.to_thread(
        search.search, filters, currency=CURRENCY, country=COUNTRY
    )

    if return_date:
        flight = _parse_rt_search_result(flights)
        if flight is None:
            return None
        return _flight_details_from_result(flight)

    if not flights:
        return None
    return _flight_details_from_result(flights[0])


async def scan_route(
    from_code: str,
    to_code: str,
    max_stops: str = "any",
    stay_days: int | None = None,
) -> ScanResult | None:
    """Full scan: date prices + flight details for cheapest day."""
    try:
        date_prices = await scan_route_dates(from_code, to_code, stay_days=stay_days)
    except Exception:
        logger.exception(f"Failed to scan dates for {from_code} -> {to_code}")
        return None

    if not date_prices:
        logger.warning(f"No prices found for {from_code} -> {to_code}")
        return None

    top_days = []
    first_details = None

    for day in date_prices:
        return_date = day.get("return_date")
        if stay_days and not return_date:
            return_date = _return_date(day["date"], stay_days)

        try:
            details = await scan_flight_details(
                from_code,
                to_code,
                day["date"],
                max_stops=max_stops,
                return_date=return_date if stay_days else None,
            )
        except Exception:
            logger.exception(f"Failed to get flight details for {day['date']}")
            continue

        if details is None:
            continue

        entry = {"date": day["date"], "price": details["price"]}
        if return_date:
            entry["return_date"] = return_date
        top_days.append(entry)
        if first_details is None:
            first_details = details

        if len(top_days) >= TOP_CHEAPEST:
            break

    if not top_days:
        logger.warning(f"No flights matching stops preference for {from_code} -> {to_code}")
        return NO_MATCHES

    calendar_prices = [d["price"] for d in date_prices]

    return ScanResult(
        from_airport=from_code.upper(),
        to_airport=to_code.upper(),
        cheapest_price=top_days[0]["price"],
        cheapest_travel_date=top_days[0]["date"],
        cheapest_return_date=top_days[0].get("return_date"),
        cheapest_airline=first_details["airline"] if first_details else None,
        cheapest_departure=first_details["departure"] if first_details else None,
        cheapest_duration=first_details["duration"] if first_details else None,
        cheapest_stops=first_details["stops"] if first_details else None,
        top_days=top_days,
        avg_price=sum(calendar_prices) / len(calendar_prices),
        min_price=min(calendar_prices),
        max_price=max(calendar_prices),
        stay_days=stay_days,
    )

"""Flight search orchestration: calendar pool → confirm → re-rank."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable

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

from bot.config import (
    CANDIDATE_POOL,
    COUNTRY,
    CURRENCY,
    DAYS_TO_SCAN,
    ENABLE_SPLIT_TICKETS,
    MAX_AIRPORT_COMBOS,
    MAX_CONCURRENT_SEARCHES,
    SEARCH_CACHE_TTL_SECS,
    SEARCH_TIMEOUT_SECS,
    TOP_CHEAPEST,
)
from bot.providers import FareProvider, get_fare_provider

logger = logging.getLogger(__name__)

# Sentinel: scan found dates but no flights matched the stops filter.
NO_MATCHES = "NO_MATCHES"

STOPS_MAP = {
    "any": MaxStops.ANY,
    "direct": MaxStops.NON_STOP,
    "1stop": MaxStops.ONE_STOP_OR_FEWER,
    "2stops": MaxStops.TWO_OR_FEWER_STOPS,
}

_search_cache: dict[str, tuple[float, object]] = {}
_cache_lock = asyncio.Lock()


@dataclass
class DayDeal:
    date: str
    price: float
    return_date: str | None = None
    from_airport: str | None = None
    to_airport: str | None = None
    airline: str | None = None
    departure: str | None = None
    duration: int | None = None
    stops: int | None = None
    fare_type: str = "roundtrip"  # roundtrip | oneway | split
    outbound_price: float | None = None
    inbound_price: float | None = None
    calendar_price: float | None = None


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
    stay_days_max: int | None = None
    cheapest_return_date: str | None = None
    currency: str = CURRENCY
    provider: str = "fli"
    fare_type: str = "oneway"
    from_airports: list[str] = field(default_factory=list)
    to_airports: list[str] = field(default_factory=list)
    cheapest_from: str | None = None
    cheapest_to: str | None = None
    outbound_price: float | None = None
    inbound_price: float | None = None
    candidates_checked: int = 0


def parse_airport_list(value: str | Iterable[str]) -> list[str]:
    if isinstance(value, str):
        parts = [p.strip().upper() for p in value.replace(";", ",").split(",")]
    else:
        parts = [str(p).strip().upper() for p in value]
    return [p for p in parts if p]


def parse_stay_range(value: str | int | None) -> tuple[int | None, int | None]:
    """Parse stay days. Accepts 10, '10', or '7-10'. Returns (min, max)."""
    if value is None:
        return None, None
    if isinstance(value, int):
        return value, value
    text = str(value).strip()
    if not text:
        return None, None
    if "-" in text:
        left, right = text.split("-", 1)
        stay_min = int(left.strip())
        stay_max = int(right.strip())
        if stay_min > stay_max:
            stay_min, stay_max = stay_max, stay_min
        return stay_min, stay_max
    days = int(text)
    return days, days


def _get_airport(code: str):
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


def _stay_length(outbound: str, inbound: str) -> int:
    return (
        datetime.strptime(inbound, "%Y-%m-%d") - datetime.strptime(outbound, "%Y-%m-%d")
    ).days


def _parse_rt_search_result(flights):
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


def _airport_pairs(from_airports: list[str], to_airports: list[str]) -> list[tuple[str, str]]:
    pairs = [(f, t) for f in from_airports for t in to_airports]
    return pairs[:MAX_AIRPORT_COMBOS]


def _cache_key(*parts) -> str:
    return "|".join(str(p) for p in parts)


async def _cached_call(key: str, coro_factory):
    now = time.monotonic()
    async with _cache_lock:
        hit = _search_cache.get(key)
        if hit and now - hit[0] < SEARCH_CACHE_TTL_SECS:
            return hit[1]

    result = await coro_factory()

    async with _cache_lock:
        _search_cache[key] = (time.monotonic(), result)
    return result


async def _run_with_timeout(func, *args, **kwargs):
    return await asyncio.wait_for(
        asyncio.to_thread(func, *args, **kwargs),
        timeout=SEARCH_TIMEOUT_SECS,
    )


async def _scan_oneway_dates(
    provider: FareProvider,
    from_code: str,
    to_code: str,
    start: datetime,
    end: datetime,
    currency: str,
    country: str,
) -> list[dict]:
    filters = DateSearchFilters(
        passenger_info=PassengerInfo(adults=1),
        flight_segments=[_make_segment(from_code, to_code, start.strftime("%Y-%m-%d"))],
        from_date=start.strftime("%Y-%m-%d"),
        to_date=end.strftime("%Y-%m-%d"),
    )
    key = _cache_key("dates", provider.name, from_code, to_code, start, end, currency, "ow")

    async def _call():
        return await _run_with_timeout(
            provider.search_dates, filters, currency=currency, country=country
        )

    results = await _cached_call(key, _call)
    if not results:
        return []

    date_prices = []
    for r in results:
        date_prices.append(
            {
                "date": r.date[0].strftime("%Y-%m-%d"),
                "price": r.price,
                "from_airport": from_code,
                "to_airport": to_code,
            }
        )
    return sorted(date_prices, key=lambda x: x["price"])


async def _scan_roundtrip_dates(
    provider: FareProvider,
    from_code: str,
    to_code: str,
    stay_days: int,
    start: datetime,
    end: datetime,
    currency: str,
    country: str,
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
        duration=stay_days,
    )
    key = _cache_key(
        "dates", provider.name, from_code, to_code, stay_days, start, end, currency, "rt"
    )

    async def _call():
        return await _run_with_timeout(
            provider.search_dates, filters, currency=currency, country=country
        )

    results = await _cached_call(key, _call)
    if not results:
        return []

    date_prices = []
    for r in results:
        outbound = r.date[0].strftime("%Y-%m-%d")
        if len(r.date) > 1:
            ret = r.date[1].strftime("%Y-%m-%d")
        else:
            ret = _return_date(outbound, stay_days)
        if _stay_length(outbound, ret) != stay_days:
            continue
        date_prices.append(
            {
                "date": outbound,
                "return_date": ret,
                "price": r.price,
                "from_airport": from_code,
                "to_airport": to_code,
                "stay_days": stay_days,
            }
        )
    return sorted(date_prices, key=lambda x: x["price"])


def _generate_outbound_dates(
    start: datetime,
    end: datetime,
    stay_days: int | None = None,
    from_code: str | None = None,
    to_code: str | None = None,
) -> list[dict]:
    dates = []
    current = start
    while current <= end:
        entry = {"date": current.strftime("%Y-%m-%d")}
        if stay_days:
            entry["return_date"] = _return_date(entry["date"], stay_days)
            entry["stay_days"] = stay_days
        if from_code:
            entry["from_airport"] = from_code
        if to_code:
            entry["to_airport"] = to_code
        dates.append(entry)
        current += timedelta(days=1)
    return dates


async def scan_route_dates(
    from_code: str,
    to_code: str,
    days: int = DAYS_TO_SCAN,
    stay_days: int | None = None,
    *,
    provider: FareProvider | None = None,
    currency: str = CURRENCY,
    country: str = COUNTRY,
) -> list[dict]:
    """Get prices for the next N days. Returns list sorted by price."""
    provider = provider or get_fare_provider()
    tomorrow = datetime.now() + timedelta(days=1)
    end_date = tomorrow + timedelta(days=days)

    if stay_days:
        date_prices = await _scan_roundtrip_dates(
            provider, from_code, to_code, stay_days, tomorrow, end_date, currency, country
        )
        if date_prices:
            return date_prices
        logger.warning(
            "Round-trip calendar empty for %s -> %s (%sd), trying outbound calendar",
            from_code,
            to_code,
            stay_days,
        )
        oneway_prices = await _scan_oneway_dates(
            provider, from_code, to_code, tomorrow, end_date, currency, country
        )
        if oneway_prices:
            for day in oneway_prices:
                day["return_date"] = _return_date(day["date"], stay_days)
                day["stay_days"] = stay_days
            return oneway_prices
        logger.warning(
            "Outbound calendar also empty for %s -> %s (%sd), using generated dates",
            from_code,
            to_code,
            stay_days,
        )
        return _generate_outbound_dates(tomorrow, end_date, stay_days, from_code, to_code)

    oneway_prices = await _scan_oneway_dates(
        provider, from_code, to_code, tomorrow, end_date, currency, country
    )
    if oneway_prices:
        return oneway_prices
    logger.warning(
        "Calendar empty for %s -> %s, using generated outbound dates",
        from_code,
        to_code,
    )
    return _generate_outbound_dates(tomorrow, end_date, from_code=from_code, to_code=to_code)


async def scan_flight_details(
    from_code: str,
    to_code: str,
    travel_date: str,
    max_stops: str = "any",
    return_date: str | None = None,
    *,
    provider: FareProvider | None = None,
    currency: str = CURRENCY,
    country: str = COUNTRY,
) -> dict | None:
    """Get flight details for a specific date. Returns cheapest flight info."""
    provider = provider or get_fare_provider()
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

    key = _cache_key(
        "flights",
        provider.name,
        from_code,
        to_code,
        travel_date,
        return_date,
        max_stops,
        currency,
    )

    async def _call():
        return await _run_with_timeout(
            provider.search_flights, filters, currency=currency, country=country
        )

    flights = await _cached_call(key, _call)

    if return_date:
        flight = _parse_rt_search_result(flights)
        if flight is None:
            return None
        return _flight_details_from_result(flight)

    if not flights:
        return None
    return _flight_details_from_result(flights[0])


async def _confirm_candidate(
    candidate: dict,
    max_stops: str,
    stay_min: int | None,
    stay_max: int | None,
    *,
    provider: FareProvider,
    currency: str,
    country: str,
    semaphore: asyncio.Semaphore,
) -> DayDeal | None:
    from_code = candidate["from_airport"]
    to_code = candidate["to_airport"]
    travel_date = candidate["date"]
    return_date = candidate.get("return_date")
    is_rt = stay_min is not None

    async with semaphore:
        try:
            details = await scan_flight_details(
                from_code,
                to_code,
                travel_date,
                max_stops=max_stops,
                return_date=return_date if is_rt else None,
                provider=provider,
                currency=currency,
                country=country,
            )
        except Exception:
            logger.exception("Failed to confirm candidate %s %s->%s", travel_date, from_code, to_code)
            return None

    if details is None:
        return None

    if is_rt and return_date:
        stay = _stay_length(travel_date, return_date)
        if stay < stay_min or stay > (stay_max or stay_min):
            return None

    deal = DayDeal(
        date=travel_date,
        price=details["price"],
        return_date=return_date if is_rt else None,
        from_airport=from_code,
        to_airport=to_code,
        airline=details["airline"],
        departure=details["departure"],
        duration=details["duration"],
        stops=details["stops"],
        fare_type="roundtrip" if is_rt else "oneway",
        calendar_price=candidate.get("price"),
    )

    if is_rt and ENABLE_SPLIT_TICKETS and return_date:
        try:
            async with semaphore:
                outbound = await scan_flight_details(
                    from_code,
                    to_code,
                    travel_date,
                    max_stops=max_stops,
                    provider=provider,
                    currency=currency,
                    country=country,
                )
            async with semaphore:
                inbound = await scan_flight_details(
                    to_code,
                    from_code,
                    return_date,
                    max_stops=max_stops,
                    provider=provider,
                    currency=currency,
                    country=country,
                )
            if outbound and inbound:
                split_price = outbound["price"] + inbound["price"]
                if split_price < deal.price:
                    deal.price = split_price
                    deal.fare_type = "split"
                    deal.outbound_price = outbound["price"]
                    deal.inbound_price = inbound["price"]
                    deal.airline = outbound.get("airline")
                    deal.departure = outbound.get("departure")
                    deal.duration = outbound.get("duration")
                    deal.stops = outbound.get("stops")
        except Exception:
            logger.exception("Split-ticket check failed for %s", travel_date)

    return deal


def _dedupe_deals(deals: list[DayDeal]) -> list[DayDeal]:
    best: dict[tuple, DayDeal] = {}
    for deal in deals:
        key = (
            deal.from_airport,
            deal.to_airport,
            deal.date,
            deal.return_date,
            deal.fare_type,
        )
        existing = best.get(key)
        if existing is None or deal.price < existing.price:
            best[key] = deal
    return sorted(best.values(), key=lambda d: d.price)


def _deal_to_dict(deal: DayDeal) -> dict:
    data = {
        "date": deal.date,
        "price": deal.price,
        "from_airport": deal.from_airport,
        "to_airport": deal.to_airport,
        "fare_type": deal.fare_type,
        "airline": deal.airline,
        "departure": deal.departure,
        "duration": deal.duration,
        "stops": deal.stops,
    }
    if deal.return_date:
        data["return_date"] = deal.return_date
    if deal.outbound_price is not None:
        data["outbound_price"] = deal.outbound_price
    if deal.inbound_price is not None:
        data["inbound_price"] = deal.inbound_price
    if deal.calendar_price is not None:
        data["calendar_price"] = deal.calendar_price
    return data


async def _collect_calendar_candidates(
    from_airports: list[str],
    to_airports: list[str],
    stay_min: int | None,
    stay_max: int | None,
    days: int,
    *,
    provider: FareProvider,
    currency: str,
    country: str,
) -> list[dict]:
    pairs = _airport_pairs(from_airports, to_airports)
    stay_values: list[int | None]
    if stay_min is None:
        stay_values = [None]
    else:
        stay_values = list(range(stay_min, (stay_max or stay_min) + 1))

    all_candidates: list[dict] = []
    for from_code, to_code in pairs:
        for stay in stay_values:
            try:
                prices = await scan_route_dates(
                    from_code,
                    to_code,
                    days=days,
                    stay_days=stay,
                    provider=provider,
                    currency=currency,
                    country=country,
                )
            except Exception:
                logger.exception("Calendar scan failed for %s->%s stay=%s", from_code, to_code, stay)
                continue
            for day in prices:
                day.setdefault("from_airport", from_code)
                day.setdefault("to_airport", to_code)
                if stay is not None:
                    day.setdefault("stay_days", stay)
                    day.setdefault("return_date", _return_date(day["date"], stay))
                all_candidates.append(day)

    # Prefer calendar-priced entries; fall back to generated dates without price.
    priced = [c for c in all_candidates if "price" in c]
    if priced:
        priced.sort(key=lambda x: x["price"])
        # Deduplicate by route+dates keeping cheapest calendar estimate
        seen = set()
        unique = []
        for c in priced:
            key = (c["from_airport"], c["to_airport"], c["date"], c.get("return_date"))
            if key in seen:
                continue
            seen.add(key)
            unique.append(c)
        return unique

    # No prices: keep generated dates unique
    seen = set()
    unique = []
    for c in all_candidates:
        key = (c.get("from_airport"), c.get("to_airport"), c["date"], c.get("return_date"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(c)
    return unique


async def scan_route(
    from_code: str | list[str],
    to_code: str | list[str],
    max_stops: str = "any",
    stay_days: int | None = None,
    stay_days_max: int | None = None,
    *,
    days: int = DAYS_TO_SCAN,
    provider: FareProvider | None = None,
    currency: str = CURRENCY,
    country: str = COUNTRY,
    candidate_pool: int = CANDIDATE_POOL,
) -> ScanResult | str | None:
    """Full scan: calendar candidates → confirm top pool → re-rank by confirmed price."""
    provider = provider or get_fare_provider()
    from_airports = parse_airport_list(from_code)
    to_airports = parse_airport_list(to_code)
    if not from_airports or not to_airports:
        return None

    stay_min = stay_days
    stay_max = stay_days_max if stay_days_max is not None else stay_days

    try:
        candidates = await _collect_calendar_candidates(
            from_airports,
            to_airports,
            stay_min,
            stay_max,
            days,
            provider=provider,
            currency=currency,
            country=country,
        )
    except Exception:
        logger.exception("Failed to scan dates for %s -> %s", from_airports, to_airports)
        return None

    if not candidates:
        logger.warning("No prices found for %s -> %s", from_airports, to_airports)
        return None

    pool = candidates[: max(candidate_pool, TOP_CHEAPEST)]
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_SEARCHES)
    tasks = [
        _confirm_candidate(
            candidate,
            max_stops,
            stay_min,
            stay_max,
            provider=provider,
            currency=currency,
            country=country,
            semaphore=semaphore,
        )
        for candidate in pool
    ]
    confirmed = await asyncio.gather(*tasks)
    deals = _dedupe_deals([d for d in confirmed if d is not None])

    if not deals:
        logger.warning("No flights matching stops preference for %s -> %s", from_airports, to_airports)
        return NO_MATCHES

    top = deals[:TOP_CHEAPEST]
    detail_prices = [d.price for d in deals]
    best = top[0]
    route_label_from = ",".join(from_airports)
    route_label_to = ",".join(to_airports)

    return ScanResult(
        from_airport=route_label_from,
        to_airport=route_label_to,
        from_airports=from_airports,
        to_airports=to_airports,
        cheapest_price=best.price,
        cheapest_travel_date=best.date,
        cheapest_return_date=best.return_date,
        cheapest_airline=best.airline,
        cheapest_departure=best.departure,
        cheapest_duration=best.duration,
        cheapest_stops=best.stops,
        cheapest_from=best.from_airport,
        cheapest_to=best.to_airport,
        top_days=[_deal_to_dict(d) for d in top],
        avg_price=sum(detail_prices) / len(detail_prices),
        min_price=min(detail_prices),
        max_price=max(detail_prices),
        stay_days=stay_min,
        stay_days_max=stay_max if stay_max != stay_min else None,
        currency=currency,
        provider=provider.name,
        fare_type=best.fare_type,
        outbound_price=best.outbound_price,
        inbound_price=best.inbound_price,
        candidates_checked=len(pool),
    )


def clear_search_cache():
    _search_cache.clear()

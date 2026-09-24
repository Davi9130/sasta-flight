"""Flight search orchestration: calendar pool → confirm → re-rank."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, NoReturn

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
    CALENDAR_CHUNK_DAYS,
    CALENDAR_CHUNK_PAUSE_SECS,
    CANDIDATE_POOL,
    COUNTRY,
    CURRENCY,
    DAYS_TO_SCAN,
    ENABLE_SPLIT_TICKETS,
    HUB_CONFIRM_POOL,
    MAX_AIRPORT_COMBOS,
    MAX_CONCURRENT_SEARCHES,
    SEARCH_429_BASE_DELAY_SECS,
    SEARCH_429_MAX_RETRIES,
    SEARCH_CACHE_TTL_SECS,
    SEARCH_CIRCUIT_COOLDOWN_SECS,
    SEARCH_CIRCUIT_THRESHOLD,
    SEARCH_MIN_INTERVAL_SECS,
    SEARCH_SILENT_BLOCK_COOLDOWN_SECS,
    SEARCH_TIMEOUT_SECS,
    STAY_SAMPLE_STEP,
    TOP_CHEAPEST,
)
from bot.providers import FareProvider, get_fare_provider

logger = logging.getLogger(__name__)

# Sentinel: scan found dates but no flights matched the stops filter.
NO_MATCHES = "NO_MATCHES"
# Sentinel: Google Flights rate-limited / circuit open.
RATE_LIMITED = "RATE_LIMITED"

STOPS_MAP = {
    "any": MaxStops.ANY,
    "direct": MaxStops.NON_STOP,
    "1stop": MaxStops.ONE_STOP_OR_FEWER,
    "2stops": MaxStops.TWO_OR_FEWER_STOPS,
}

_search_cache: dict[str, tuple[float, object]] = {}
_cache_lock = asyncio.Lock()

# Global rate limiter state
_rate_lock = asyncio.Lock()
_last_request_at = 0.0
_consecutive_429 = 0
_circuit_open_until = 0.0
_circuit_reason: str | None = None


class RateLimitPaused(Exception):
    """Circuit breaker is open; scans should stop briefly."""

    def __init__(self, retry_after_secs: float):
        self.retry_after_secs = retry_after_secs
        super().__init__(f"Rate limit circuit open for {retry_after_secs:.0f}s")


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
    direct_price: float | None = None
    hub_nights: int | None = None
    hub_nights_max: int | None = None
    via_combos: list[dict] = field(default_factory=list)


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


def _flight_details_from_result(flight) -> dict | None:
    if flight is None or getattr(flight, "price", None) is None:
        return None
    legs = getattr(flight, "legs", None) or []
    leg = legs[0] if legs else None
    last = legs[-1] if legs else None
    airline = None
    if leg is not None:
        airline_obj = getattr(leg, "airline", None)
        airline = getattr(airline_obj, "value", airline_obj)
        if not airline:
            airline = getattr(flight, "primary_airline_name", None)
    departure = None
    departure_at = getattr(leg, "departure_datetime", None) if leg is not None else None
    arrival_at = getattr(last, "arrival_datetime", None) if last is not None else None
    if departure_at:
        departure = departure_at.strftime("%I:%M %p")
    return {
        "price": flight.price,
        "airline": airline,
        "departure": departure,
        "departure_at": departure_at,
        "arrival_at": arrival_at,
        "duration": flight.duration,
        "stops": flight.stops,
    }


def _airport_pairs(from_airports: list[str], to_airports: list[str]) -> list[tuple[str, str]]:
    pairs = [(f, t) for f in from_airports for t in to_airports]
    return pairs[:MAX_AIRPORT_COMBOS]


def _cache_key(*parts) -> str:
    return "|".join(str(p) for p in parts)


def _payload_label(result) -> str:
    if result is None:
        return "None"
    if isinstance(result, (list, tuple)):
        return f"{type(result).__name__}[{len(result)}]"
    return type(result).__name__


def _is_rate_limit_error(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    if status in {401, 403, 429, 503}:
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(token in text for token in ("429", "403", "401", "captcha", "forbidden")):
        return True
    if "rate" in text and ("limit" in text or "blocked" in text):
        return True
    cause = getattr(exc, "__cause__", None)
    if cause is not None and cause is not exc:
        return _is_rate_limit_error(cause)
    return False


def circuit_retry_after_secs() -> float:
    remaining = _circuit_open_until - time.monotonic()
    return max(0.0, remaining)


def is_circuit_open() -> bool:
    return circuit_retry_after_secs() > 0


def get_circuit_reason() -> str | None:
    """Why the circuit is open: http_429 | silent_empty_calendar | http_block, or None."""
    if not is_circuit_open():
        return None
    return _circuit_reason


def reset_rate_limiter():
    """Test helper: clear limiter / circuit state."""
    global _last_request_at, _consecutive_429, _circuit_open_until, _circuit_reason
    _last_request_at = 0.0
    _consecutive_429 = 0
    _circuit_open_until = 0.0
    _circuit_reason = None


def _open_circuit(reason: str, cooldown_secs: float) -> None:
    global _circuit_open_until, _circuit_reason
    _circuit_reason = reason
    _circuit_open_until = time.monotonic() + cooldown_secs
    logger.error(
        "Circuit OPEN reason=%s cooldown=%.0fs",
        reason,
        cooldown_secs,
    )


def _mark_silent_block(reason: str) -> float:
    """Treat empty calendar/payload as a soft Google block. Returns cooldown secs."""
    if is_circuit_open():
        remaining = circuit_retry_after_secs()
        logger.warning(
            "SILENT_BLOCK (circuit already open reason=%s retry_after=%.0fs): %s",
            _circuit_reason,
            remaining,
            reason,
        )
        return remaining
    cooldown = SEARCH_SILENT_BLOCK_COOLDOWN_SECS
    logger.error(
        "SILENT_BLOCK detected: %s — empty payload without HTTP 429; pausing %.0fs",
        reason,
        cooldown,
    )
    _open_circuit("silent_empty_calendar", cooldown)
    return cooldown


def _raise_silent_block(reason: str) -> NoReturn:
    cooldown = _mark_silent_block(reason)
    raise RateLimitPaused(cooldown)


def _sample_stay_values(stay_min: int, stay_max: int) -> list[int]:
    step = STAY_SAMPLE_STEP
    values = list(range(stay_min, stay_max + 1, step))
    if stay_max not in values:
        values.append(stay_max)
    return values


def _date_chunks(start: datetime, end: datetime, chunk_days: int) -> list[tuple[datetime, datetime]]:
    """Inclusive date range split into chunks of at most chunk_days span."""
    chunks: list[tuple[datetime, datetime]] = []
    current = start
    while current <= end:
        chunk_end = min(current + timedelta(days=chunk_days), end)
        chunks.append((current, chunk_end))
        current = chunk_end + timedelta(days=1)
    return chunks


async def _wait_for_rate_slot():
    global _last_request_at
    async with _rate_lock:
        now = time.monotonic()
        if now < _circuit_open_until:
            raise RateLimitPaused(_circuit_open_until - now)
        wait = SEARCH_MIN_INTERVAL_SECS - (now - _last_request_at)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_at = time.monotonic()


def _note_success():
    global _consecutive_429
    _consecutive_429 = 0


def _note_429() -> float:
    """Record a 429 and possibly open the circuit. Returns backoff delay secs."""
    global _consecutive_429
    _consecutive_429 += 1
    attempt = min(_consecutive_429, SEARCH_429_MAX_RETRIES)
    delay = SEARCH_429_BASE_DELAY_SECS * (2 ** (attempt - 1))
    delay *= 0.8 + random.random() * 0.4  # jitter
    if _consecutive_429 >= SEARCH_CIRCUIT_THRESHOLD:
        _open_circuit("http_429", SEARCH_CIRCUIT_COOLDOWN_SECS)
        logger.warning(
            "HTTP 429 threshold reached (%d consecutive); circuit cooldown %.0fs",
            _consecutive_429,
            SEARCH_CIRCUIT_COOLDOWN_SECS,
        )
    return delay


async def _provider_call(func, *args, **kwargs):
    """Call provider with min-interval gating, 429 retry/backoff, and circuit breaker."""
    last_exc: BaseException | None = None
    for attempt in range(SEARCH_429_MAX_RETRIES + 1):
        await _wait_for_rate_slot()
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(func, *args, **kwargs),
                timeout=SEARCH_TIMEOUT_SECS,
            )
            if result is None or result == []:
                logger.warning(
                    "Provider returned empty payload fn=%s payload=%s",
                    getattr(func, "__name__", func),
                    _payload_label(result),
                )
                return result
            _note_success()
            return result
        except RateLimitPaused:
            raise
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            logger.warning(
                "Provider call failed fn=%s type=%s status=%s err=%s",
                getattr(func, "__name__", func),
                type(exc).__name__,
                status,
                exc,
            )
            if status in {401, 403, 503} and not is_circuit_open():
                _open_circuit("http_block", SEARCH_SILENT_BLOCK_COOLDOWN_SECS)
            if not _is_rate_limit_error(exc):
                raise
            last_exc = exc
            if attempt >= SEARCH_429_MAX_RETRIES:
                break
            delay = _note_429()
            if is_circuit_open():
                raise RateLimitPaused(circuit_retry_after_secs()) from exc
            logger.warning(
                "Google Flights 429 (attempt %d/%d), backing off %.1fs",
                attempt + 1,
                SEARCH_429_MAX_RETRIES + 1,
                delay,
            )
            await asyncio.sleep(delay)
    assert last_exc is not None
    _note_429()
    if is_circuit_open():
        raise RateLimitPaused(circuit_retry_after_secs()) from last_exc
    raise last_exc


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


async def _scan_oneway_dates_window(
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
        return await _provider_call(
            provider.search_dates, filters, currency=currency, country=country
        )

    results = await _cached_call(key, _call)
    if not results:
        logger.warning(
            "SearchDates empty trip=OW %s->%s window=%s..%s payload=%s",
            from_code,
            to_code,
            start.strftime("%Y-%m-%d"),
            end.strftime("%Y-%m-%d"),
            _payload_label(results),
        )
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
    logger.info(
        "SearchDates OW %s->%s window=%s..%s days=%d",
        from_code,
        to_code,
        start.strftime("%Y-%m-%d"),
        end.strftime("%Y-%m-%d"),
        len(date_prices),
    )
    return sorted(date_prices, key=lambda x: x["price"])


async def _scan_roundtrip_dates_window(
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
        return await _provider_call(
            provider.search_dates, filters, currency=currency, country=country
        )

    results = await _cached_call(key, _call)
    if not results:
        logger.warning(
            "SearchDates empty trip=RT %s->%s stay=%sd window=%s..%s payload=%s",
            from_code,
            to_code,
            stay_days,
            start.strftime("%Y-%m-%d"),
            end.strftime("%Y-%m-%d"),
            _payload_label(results),
        )
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


async def _scan_dates_chunked(
    *,
    provider: FareProvider,
    from_code: str,
    to_code: str,
    start: datetime,
    end: datetime,
    stay_days: int | None,
    currency: str,
    country: str,
) -> list[dict]:
    span_days = (end - start).days
    chunks = _date_chunks(start, end, CALENDAR_CHUNK_DAYS) if span_days > CALENDAR_CHUNK_DAYS else [(start, end)]
    merged: list[dict] = []
    for i, (c_start, c_end) in enumerate(chunks):
        if stay_days:
            part = await _scan_roundtrip_dates_window(
                provider, from_code, to_code, stay_days, c_start, c_end, currency, country
            )
        else:
            part = await _scan_oneway_dates_window(
                provider, from_code, to_code, c_start, c_end, currency, country
            )
        merged.extend(part)
        if i < len(chunks) - 1 and CALENDAR_CHUNK_PAUSE_SECS > 0:
            await asyncio.sleep(CALENDAR_CHUNK_PAUSE_SECS)
    if not merged:
        return []
    return sorted(merged, key=lambda x: x["price"])


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
    """Get prices for the next N days. Empty calendar is a silent block, not fake dates."""
    provider = provider or get_fare_provider()
    tomorrow = datetime.now() + timedelta(days=1)
    end_date = tomorrow + timedelta(days=days)

    if stay_days:
        date_prices = await _scan_dates_chunked(
            provider=provider,
            from_code=from_code,
            to_code=to_code,
            start=tomorrow,
            end=end_date,
            stay_days=stay_days,
            currency=currency,
            country=country,
        )
        if date_prices:
            return date_prices
        logger.warning(
            "Round-trip calendar empty for %s -> %s (%sd); trying outbound calendar once",
            from_code,
            to_code,
            stay_days,
        )
        oneway_prices = await _scan_dates_chunked(
            provider=provider,
            from_code=from_code,
            to_code=to_code,
            start=tomorrow,
            end=end_date,
            stay_days=None,
            currency=currency,
            country=country,
        )
        if oneway_prices:
            logger.info(
                "Using outbound calendar as RT proxy for %s -> %s (%sd); %d days",
                from_code,
                to_code,
                stay_days,
                len(oneway_prices),
            )
            for day in oneway_prices:
                day["return_date"] = _return_date(day["date"], stay_days)
                day["stay_days"] = stay_days
            return oneway_prices
        _raise_silent_block(
            f"empty calendar RT+OW {from_code}->{to_code} stay={stay_days}d"
        )

    oneway_prices = await _scan_dates_chunked(
        provider=provider,
        from_code=from_code,
        to_code=to_code,
        start=tomorrow,
        end=end_date,
        stay_days=None,
        currency=currency,
        country=country,
    )
    if oneway_prices:
        return oneway_prices
    _raise_silent_block(f"empty calendar OW {from_code}->{to_code}")


# Back-compat aliases used by older tests
async def _scan_oneway_dates(provider, from_code, to_code, start, end, currency, country):
    return await _scan_oneway_dates_window(
        provider, from_code, to_code, start, end, currency, country
    )


async def _scan_roundtrip_dates(provider, from_code, to_code, stay_days, start, end, currency, country):
    return await _scan_roundtrip_dates_window(
        provider, from_code, to_code, stay_days, start, end, currency, country
    )


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
        return await _provider_call(
            provider.search_flights, filters, currency=currency, country=country
        )

    flights = await _cached_call(key, _call)

    if not flights:
        logger.warning(
            "SearchFlights empty %s->%s date=%s return=%s stops=%s payload=%s",
            from_code,
            to_code,
            travel_date,
            return_date,
            max_stops,
            _payload_label(flights),
        )
        return None

    if return_date:
        details = _flight_details_from_result(_parse_rt_search_result(flights))
        if details is None:
            logger.warning(
                "SearchFlights RT parsed empty %s->%s date=%s return=%s payload=%s",
                from_code,
                to_code,
                travel_date,
                return_date,
                _payload_label(flights),
            )
        return details

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
    check_split: bool = False,
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
        except RateLimitPaused:
            raise
        except Exception as exc:
            if _is_rate_limit_error(exc):
                logger.warning(
                    "Rate limited confirming %s %s->%s: %s",
                    travel_date,
                    from_code,
                    to_code,
                    exc,
                )
                raise RateLimitPaused(circuit_retry_after_secs() or SEARCH_CIRCUIT_COOLDOWN_SECS) from exc
            logger.warning(
                "Failed to confirm candidate %s %s->%s: %s",
                travel_date,
                from_code,
                to_code,
                exc,
            )
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

    if check_split and is_rt and ENABLE_SPLIT_TICKETS and return_date:
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
        except RateLimitPaused:
            raise
        except Exception as exc:
            logger.warning("Split-ticket check failed for %s: %s", travel_date, exc)

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


async def _maybe_apply_split(
    deals: list[DayDeal],
    *,
    max_stops: str,
    provider: FareProvider,
    currency: str,
    country: str,
    semaphore: asyncio.Semaphore,
) -> list[DayDeal]:
    """Only check split tickets on the current cheapest deal to limit extra calls."""
    if not ENABLE_SPLIT_TICKETS or not deals:
        return deals
    best = deals[0]
    if best.fare_type != "roundtrip" or not best.return_date:
        return deals
    candidate = {
        "from_airport": best.from_airport,
        "to_airport": best.to_airport,
        "date": best.date,
        "return_date": best.return_date,
        "price": best.calendar_price,
    }
    updated = await _confirm_candidate(
        candidate,
        max_stops,
        _stay_length(best.date, best.return_date),
        _stay_length(best.date, best.return_date),
        provider=provider,
        currency=currency,
        country=country,
        semaphore=semaphore,
        check_split=True,
    )
    if updated and updated.fare_type == "split" and updated.price < best.price:
        deals[0] = updated
        deals.sort(key=lambda d: d.price)
    return deals


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
        stay_values = _sample_stay_values(stay_min, stay_max or stay_min)

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
            except RateLimitPaused:
                raise
            except Exception as exc:
                if _is_rate_limit_error(exc):
                    raise RateLimitPaused(
                        circuit_retry_after_secs() or SEARCH_CIRCUIT_COOLDOWN_SECS
                    ) from exc
                logger.warning(
                    "Calendar scan failed for %s->%s stay=%s: %s",
                    from_code,
                    to_code,
                    stay,
                    exc,
                )
                continue
            for day in prices:
                day.setdefault("from_airport", from_code)
                day.setdefault("to_airport", to_code)
                if stay is not None:
                    day.setdefault("stay_days", stay)
                    day.setdefault("return_date", _return_date(day["date"], stay))
                all_candidates.append(day)

    priced = [c for c in all_candidates if "price" in c]
    if priced:
        priced.sort(key=lambda x: x["price"])
        seen = set()
        unique = []
        for c in priced:
            key = (c["from_airport"], c["to_airport"], c["date"], c.get("return_date"))
            if key in seen:
                continue
            seen.add(key)
            unique.append(c)
        return unique

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
    if is_circuit_open():
        return RATE_LIMITED

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
    except RateLimitPaused:
        return RATE_LIMITED
    except Exception:
        logger.exception("Failed to scan dates for %s -> %s", from_airports, to_airports)
        return None

    if not candidates:
        logger.error(
            "SILENT_BLOCK no calendar candidates for %s -> %s after collection",
            from_airports,
            to_airports,
        )
        _mark_silent_block(f"no calendar candidates {from_airports}->{to_airports}")
        return RATE_LIMITED

    pool = candidates[: max(candidate_pool, TOP_CHEAPEST)]
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_SEARCHES)
    confirmed: list[DayDeal] = []
    rate_limited = False
    # Confirm with bounded concurrency; stop early on circuit open but keep partials.
    for candidate in pool:
        try:
            deal = await _confirm_candidate(
                candidate,
                max_stops,
                stay_min,
                stay_max,
                provider=provider,
                currency=currency,
                country=country,
                semaphore=semaphore,
                check_split=False,
            )
        except RateLimitPaused:
            rate_limited = True
            break
        if deal is not None:
            confirmed.append(deal)

    deals = _dedupe_deals(confirmed)
    if not deals:
        if rate_limited or is_circuit_open():
            return RATE_LIMITED
        if max_stops == "any":
            _mark_silent_block(
                f"all {len(pool)} detail confirms empty with stops=any "
                f"{from_airports}->{to_airports}"
            )
            return RATE_LIMITED
        logger.warning(
            "No flights matching stops preference=%s for %s -> %s (checked %d candidates)",
            max_stops,
            from_airports,
            to_airports,
            len(pool),
        )
        return NO_MATCHES

    try:
        deals = await _maybe_apply_split(
            deals,
            max_stops=max_stops,
            provider=provider,
            currency=currency,
            country=country,
            semaphore=semaphore,
        )
    except RateLimitPaused:
        pass  # keep package fares

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


def _quotes_from_days(days: list[dict]) -> list:
    from bot.hubs import LegQuote

    quotes = []
    for day in days:
        if day.get("price") is None or not day.get("date"):
            continue
        quotes.append(
            LegQuote(
                date=day["date"],
                price=float(day["price"]),
                from_airport=day.get("from_airport") or "",
                to_airport=day.get("to_airport") or "",
            )
        )
    return quotes


async def _calendar_quotes(provider, pairs, start, end, currency, country) -> list:
    quotes = []
    for from_code, to_code in pairs:
        if is_circuit_open():
            raise RateLimitPaused(circuit_retry_after_secs() or SEARCH_CIRCUIT_COOLDOWN_SECS)
        try:
            days = await _scan_dates_chunked(
                provider=provider,
                from_code=from_code,
                to_code=to_code,
                start=start,
                end=end,
                stay_days=None,
                currency=currency,
                country=country,
            )
        except RateLimitPaused:
            raise
        except Exception as exc:
            if _is_rate_limit_error(exc):
                raise RateLimitPaused(
                    circuit_retry_after_secs() or SEARCH_CIRCUIT_COOLDOWN_SECS
                ) from exc
            logger.warning("Via calendar failed %s->%s: %s", from_code, to_code, exc)
            continue
        for day in days:
            day.setdefault("from_airport", from_code)
            day.setdefault("to_airport", to_code)
        quotes.extend(_quotes_from_days(days))
    return quotes


def _leg_dict(quote, details: dict | None) -> dict:
    data = {
        "date": quote.date,
        "price": details["price"] if details else quote.price,
        "from_airport": quote.from_airport,
        "to_airport": quote.to_airport,
        "airline": (details or {}).get("airline"),
        "departure": (details or {}).get("departure"),
        "duration": (details or {}).get("duration"),
        "stops": (details or {}).get("stops"),
        "fare_type": "oneway",
    }
    return data


async def _confirm_leg(quote, max_stops, provider, currency, country, semaphore):
    async with semaphore:
        return await scan_flight_details(
            quote.from_airport,
            quote.to_airport,
            quote.date,
            max_stops=max_stops,
            provider=provider,
            currency=currency,
            country=country,
        )


def _combo_to_day(combo, legs: list[dict], total: float) -> dict:
    return {
        "date": combo.long_out.date,
        "return_date": combo.long_in.date,
        "price": total,
        "from_airport": combo.pos_out.from_airport,
        "to_airport": combo.long_out.to_airport,
        "fare_type": "via_hub",
        "airline": legs[1].get("airline") if len(legs) > 1 else None,
        "nights_out": combo.nights_out,
        "nights_back": combo.nights_back,
        "dest_stay": combo.dest_stay,
        "hub_out": combo.hub_out_city,
        "hub_back": combo.hub_back_city,
        "legs": legs,
    }


async def scan_via_route(
    from_code: str | list[str],
    to_code: str | list[str],
    stay_days: int,
    stay_days_max: int | None = None,
    hub_nights: int = 0,
    hub_nights_max: int | None = None,
    max_stops: str = "any",
    *,
    days: int = DAYS_TO_SCAN,
    provider: FareProvider | None = None,
    currency: str = CURRENCY,
    country: str = COUNTRY,
    confirm_pool: int = HUB_CONFIRM_POOL,
) -> ScanResult | str | None:
    """Join origin→hub→dest and dest→hub→origin, then confirm the cheapest totals."""
    from bot.hubs import connection_ok, join_hub_itineraries, longhaul_airports, positioning_airports

    if is_circuit_open():
        return RATE_LIMITED

    provider = provider or get_fare_provider()
    origins = parse_airport_list(from_code)
    destinations = parse_airport_list(to_code)
    if not origins or not destinations:
        return None

    stay_min = stay_days
    stay_max = stay_days_max if stay_days_max is not None else stay_days
    nights_min = hub_nights
    nights_max = hub_nights_max if hub_nights_max is not None else hub_nights

    tomorrow = datetime.now() + timedelta(days=1)
    end_date = tomorrow + timedelta(days=days + nights_max + stay_max + nights_max)
    pos_airports = positioning_airports()
    long_airports = longhaul_airports()

    try:
        pos_out = await _calendar_quotes(
            provider,
            _airport_pairs(origins, pos_airports),
            tomorrow,
            tomorrow + timedelta(days=days),
            currency,
            country,
        )
        long_out = await _calendar_quotes(
            provider,
            _airport_pairs(long_airports, destinations),
            tomorrow,
            tomorrow + timedelta(days=days + nights_max),
            currency,
            country,
        )
        long_in = await _calendar_quotes(
            provider,
            _airport_pairs(destinations, long_airports),
            tomorrow + timedelta(days=stay_min),
            end_date,
            currency,
            country,
        )
        pos_in = await _calendar_quotes(
            provider,
            _airport_pairs(pos_airports, origins),
            tomorrow + timedelta(days=stay_min),
            end_date,
            currency,
            country,
        )
    except RateLimitPaused:
        return RATE_LIMITED
    except Exception:
        logger.exception("Via calendar scan failed %s -> %s", origins, destinations)
        return None

    combos = join_hub_itineraries(
        pos_out,
        long_out,
        long_in,
        pos_in,
        dest_stay_min=stay_min,
        dest_stay_max=stay_max,
        hub_nights_min=nights_min,
        hub_nights_max=nights_max,
    )
    if not combos:
        logger.warning("No via-hub calendar combos for %s -> %s", origins, destinations)
        return NO_MATCHES

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_SEARCHES)
    confirmed_days: list[dict] = []
    rate_limited = False
    for combo in combos[: max(confirm_pool, 1)]:
        quotes = (combo.pos_out, combo.long_out, combo.long_in, combo.pos_in)
        details = []
        try:
            for quote in quotes:
                details.append(
                    await _confirm_leg(
                        quote, max_stops, provider, currency, country, semaphore
                    )
                )
        except RateLimitPaused:
            rate_limited = True
            break
        except Exception as exc:
            if _is_rate_limit_error(exc):
                rate_limited = True
                break
            logger.warning("Via confirm failed: %s", exc)
            continue
        if any(item is None for item in details):
            continue
        if not connection_ok(
            details[0].get("arrival_at"),
            details[1].get("departure_at"),
            combo.nights_out,
        ):
            continue
        if not connection_ok(
            details[2].get("arrival_at"),
            details[3].get("departure_at"),
            combo.nights_back,
        ):
            continue
        legs = [_leg_dict(quote, item) for quote, item in zip(quotes, details)]
        total = sum(leg["price"] for leg in legs)
        confirmed_days.append(_combo_to_day(combo, legs, total))

    if not confirmed_days:
        if rate_limited or is_circuit_open():
            return RATE_LIMITED
        return NO_MATCHES

    confirmed_days.sort(key=lambda day: day["price"])
    top = confirmed_days[:TOP_CHEAPEST]
    prices = [day["price"] for day in confirmed_days]
    best = top[0]

    direct_price = None
    if not is_circuit_open():
        try:
            direct = await scan_route(
                origins,
                destinations,
                max_stops=max_stops,
                stay_days=stay_min,
                stay_days_max=stay_max,
                days=days,
                provider=provider,
                currency=currency,
                country=country,
                candidate_pool=min(CANDIDATE_POOL, 3),
            )
            if isinstance(direct, ScanResult):
                direct_price = direct.cheapest_price
        except RateLimitPaused:
            pass
        except Exception:
            logger.warning("Direct comparison failed for %s -> %s", origins, destinations)

    return ScanResult(
        from_airport=",".join(origins),
        to_airport=",".join(destinations),
        from_airports=origins,
        to_airports=destinations,
        cheapest_price=best["price"],
        cheapest_travel_date=best["date"],
        cheapest_return_date=best.get("return_date"),
        cheapest_airline=best.get("airline"),
        cheapest_departure=None,
        cheapest_duration=None,
        cheapest_stops=None,
        cheapest_from=best.get("from_airport"),
        cheapest_to=best.get("to_airport"),
        top_days=top,
        avg_price=sum(prices) / len(prices),
        min_price=min(prices),
        max_price=max(prices),
        stay_days=stay_min,
        stay_days_max=stay_max if stay_max != stay_min else None,
        currency=currency,
        provider=provider.name,
        fare_type="via_hub",
        candidates_checked=min(len(combos), confirm_pool),
        direct_price=direct_price,
        hub_nights=nights_min,
        hub_nights_max=nights_max if nights_max != nights_min else None,
        via_combos=top,
    )


def clear_search_cache():
    _search_cache.clear()

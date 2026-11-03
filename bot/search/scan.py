"""Scan one route: one-way or round trip, fixed or flexible stay, multiple airports, split tickets."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from bot.config import (
    COARSE_BUDGET_SHARE,
    COARSE_STEP_DAYS,
    DAYS_TO_SCAN,
    ENABLE_SPLIT_TICKETS,
    MAX_AIRPORT_COMBOS,
    REFINE_RADIUS_DAYS,
    REFINE_TOP_K,
    REQ_BUDGET_PER_SCAN,
    SPLIT_CHECK_TOP,
    TOP_CHEAPEST,
)
from bot.fares.client import booking_url
from bot.fares.models import Quote
from bot.fares.service import FareService, ScanBudget
from bot.search.planner import (
    Planner,
    Probe,
    coarse_probes,
    sample_stays,
    stay_length,
    window_dates,
)

logger = logging.getLogger(__name__)


class _NoMatches:
    def __repr__(self) -> str:
        return "NO_MATCHES"


NO_MATCHES = _NoMatches()


class ScanDeferred(Exception):
    """Google is unavailable (block/cooldown/budget) and we have nothing usable."""

    def __init__(self, reason: str, retry_after: float, requests_used: int = 0):
        super().__init__(f"{reason}; retry in {retry_after:.0f}s")
        self.reason = reason
        self.retry_after = retry_after
        self.requests_used = requests_used


class ScanFailed(Exception):
    """Every request failed for non-block reasons."""


@dataclass
class ScanResult:
    from_airport: str
    to_airport: str
    from_airports: list[str]
    to_airports: list[str]
    currency: str
    provider: str
    fare_type: str
    top_days: list[dict]
    avg_price: float
    min_price: float
    max_price: float
    stay_days: int | None = None
    stay_days_max: int | None = None
    requests_used: int = 0
    probes_checked: int = 0
    partial: bool = False
    partial_reason: str | None = None
    hub_nights: int | None = None
    hub_nights_max: int | None = None
    direct_price: float | None = None
    via_combos: list[dict] = field(default_factory=list)

    @property
    def cheapest(self) -> dict:
        return self.top_days[0]

    @property
    def cheapest_price(self) -> float:
        return self.cheapest["price"]

    @property
    def cheapest_travel_date(self) -> str:
        return self.cheapest["date"]

    @property
    def cheapest_return_date(self) -> str | None:
        return self.cheapest.get("return_date")

    @property
    def cheapest_airline(self) -> str | None:
        return self.cheapest.get("airline")

    @property
    def cheapest_departure(self) -> str | None:
        return self.cheapest.get("departure")

    @property
    def cheapest_duration(self) -> int | None:
        return self.cheapest.get("duration")

    @property
    def cheapest_stops(self) -> int | None:
        return self.cheapest.get("stops")

    @property
    def cheapest_from(self) -> str | None:
        return self.cheapest.get("from_airport")

    @property
    def cheapest_to(self) -> str | None:
        return self.cheapest.get("to_airport")

    @property
    def outbound_price(self) -> float | None:
        return self.cheapest.get("outbound_price")

    @property
    def inbound_price(self) -> float | None:
        return self.cheapest.get("inbound_price")


def parse_airport_list(value: str | Iterable[str]) -> list[str]:
    raw = value.split(",") if isinstance(value, str) else list(value)
    seen: list[str] = []
    for item in raw:
        code = item.strip().upper()
        if code and code not in seen:
            seen.append(code)
    return seen


def parse_stay_range(value: str | int | None) -> tuple[int | None, int | None]:
    """'10' -> (10, 10); '7-10' -> (7, 10); None -> (None, None)."""
    if value is None:
        return None, None
    if isinstance(value, int):
        return value, value
    text = str(value).strip()
    if not text:
        return None, None
    if "-" in text:
        lo, hi = text.split("-", 1)
        lo_i, hi_i = int(lo), int(hi)
        if lo_i > hi_i:
            lo_i, hi_i = hi_i, lo_i
        return lo_i, hi_i
    n = int(text)
    return n, n


def airport_pairs(
    origins: list[str],
    destinations: list[str],
    priority: dict[tuple[str, str], float] | None = None,
    limit: int = MAX_AIRPORT_COMBOS,
) -> list[tuple[str, str]]:
    """Never-seen pairs first (they deserve one look), then cheapest-known first."""
    pairs = [(f, t) for f in origins for t in destinations if f != t]
    priority = priority or {}
    pairs.sort(key=lambda p: (p in priority, priority.get(p, 0.0)))
    return pairs[:limit]


def _fmt_time(value: datetime | None) -> str | None:
    return value.strftime("%I:%M %p") if value else None


def quote_to_day(quote: Quote, fare_type: str) -> dict:
    day = {
        "date": quote.date,
        "return_date": quote.return_date,
        "price": quote.price,
        "from_airport": quote.from_airport,
        "to_airport": quote.to_airport,
        "airline": quote.airline,
        "departure": _fmt_time(quote.departure_at),
        "departure_at": quote.departure_at.isoformat() if quote.departure_at else None,
        "arrival_at": quote.arrival_at.isoformat() if quote.arrival_at else None,
        "duration": quote.duration,
        "stops": quote.stops,
        "fare_type": fare_type,
        "booking_url": quote.booking_url,
    }
    if quote.return_date:
        day["stay_days"] = stay_length(quote.date, quote.return_date)
    return day


def _dedupe_days(days: list[dict]) -> list[dict]:
    best: dict[tuple, dict] = {}
    for day in days:
        key = (day["date"], day.get("return_date"))
        if key not in best or day["price"] < best[key]["price"]:
            best[key] = day
    return sorted(best.values(), key=lambda d: d["price"])


async def _apply_split(
    planner: Planner, days: list[dict], *, max_stops: str, currency: str, top: int
) -> list[dict]:
    """Replace a round trip by two one-ways when that's cheaper. Costs 2 requests per candidate."""
    out = list(days)
    for i, day in enumerate(days[:top]):
        if planner.halted or not day.get("return_date"):
            break
        f, t = day["from_airport"], day["to_airport"]
        there = await planner.cheapest(Probe(f, t, day["date"]))
        back = await planner.cheapest(Probe(t, f, day["return_date"]))
        if not there or not back:
            continue
        total = there.price + back.price
        if total >= day["price"]:
            continue
        split = dict(day)
        split.update(
            price=total,
            fare_type="split",
            outbound_price=there.price,
            inbound_price=back.price,
            airline=there.airline,
            departure=_fmt_time(there.departure_at),
            duration=there.duration,
            stops=there.stops,
            out_url=there.booking_url
            or booking_url(f, t, day["date"], max_stops=max_stops, currency=currency),
            in_url=back.booking_url
            or booking_url(t, f, day["return_date"], max_stops=max_stops, currency=currency),
        )
        out[i] = split
    return _dedupe_days(out)


def summarize(days: list[dict]) -> tuple[float, float, float]:
    prices = [d["price"] for d in days]
    return sum(prices) / len(prices), min(prices), max(prices)


async def scan_route(
    fares: FareService,
    *,
    origins: list[str],
    destinations: list[str],
    stay_min: int | None = None,
    stay_max: int | None = None,
    max_stops: str = "any",
    currency: str,
    days: int = DAYS_TO_SCAN,
    budget_limit: int = REQ_BUDGET_PER_SCAN,
    pair_priority: dict[tuple[str, str], float] | None = None,
    split_tickets: bool = ENABLE_SPLIT_TICKETS,
    today: date | None = None,
) -> ScanResult | _NoMatches:
    round_trip = stay_min is not None
    start = (today or date.today()) + timedelta(days=1)
    dates = window_dates(start, days)
    pairs = airport_pairs(origins, destinations, pair_priority)
    stays: list[int | None] = sample_stays(stay_min, stay_max) if round_trip else [None]

    budget = ScanBudget(budget_limit)
    planner = Planner(fares, budget=budget, max_stops=max_stops, currency=currency)
    reserve = 2 * SPLIT_CHECK_TOP if (split_tickets and round_trip) else 0
    coarse_budget = max(1, int((budget_limit - reserve) * COARSE_BUDGET_SHARE))
    await planner.run_all(
        coarse_probes(pairs, dates, stays, budget=coarse_budget, base_step=COARSE_STEP_DAYS)
    )
    if not planner.halted:
        # Leave room for split checks before refining eats everything.
        refine_budget = ScanBudget(max(0, budget.remaining - reserve))
        planner.budget = refine_budget
        await planner.refine(
            window=set(dates),
            stay_min=stay_min,
            stay_max=stay_max,
            top_k=REFINE_TOP_K,
            radius=REFINE_RADIUS_DAYS,
            round_trip=round_trip,
        )
        budget.used += refine_budget.used
        planner.budget = budget
        planner.exhausted = False

    ranked = planner.ranked(round_trip=round_trip)
    if not ranked:
        if planner.stopped is not None:
            raise ScanDeferred(planner.stopped.reason, planner.stopped.retry_after, budget.used)
        if planner.failures and planner.failures >= len(planner.options):
            raise ScanFailed(f"{planner.failures} request(s) failed")
        return NO_MATCHES

    fare_type = "roundtrip" if round_trip else "oneway"
    all_days = _dedupe_days([quote_to_day(q, fare_type) for _, q in ranked])
    if split_tickets and round_trip and not planner.halted:
        all_days = await _apply_split(
            planner, all_days, max_stops=max_stops, currency=currency, top=SPLIT_CHECK_TOP
        )

    avg, low, high = summarize(all_days)
    return ScanResult(
        from_airport=",".join(origins),
        to_airport=",".join(destinations),
        from_airports=origins,
        to_airports=destinations,
        currency=currency,
        provider=fares.name,
        fare_type=all_days[0]["fare_type"],
        top_days=all_days[:TOP_CHEAPEST],
        avg_price=avg,
        min_price=low,
        max_price=high,
        stay_days=stay_min,
        stay_days_max=stay_max,
        requests_used=budget.used,
        probes_checked=len(planner.options),
        partial=planner.stopped is not None,
        partial_reason=planner.stopped.reason if planner.stopped else None,
    )

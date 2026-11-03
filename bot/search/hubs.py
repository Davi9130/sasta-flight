"""Separate tickets through São Paulo or Rio: positioning leg + long-haul leg, each way."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from bot.config import (
    COARSE_STEP_DAYS,
    DAYS_TO_SCAN,
    MAX_AIRPORT_COMBOS,
    MAX_HUB_NIGHTS,
    MAX_STAY_DAYS,
    MIN_SELF_TRANSFER_HOURS,
    MIN_STAY_DAYS,
    TOP_CHEAPEST,
    VIA_BUDGET_PER_SCAN,
    VIA_LONGHAUL_HUBS,
)
from bot.fares.models import Quote
from bot.fares.service import FareService, ScanBudget
from bot.search.planner import (
    Planner,
    Probe,
    add_days,
    coarse_probes,
    sample_stays,
    stay_length,
    window_dates,
)
from bot.search.scan import (
    NO_MATCHES,
    ScanDeferred,
    ScanFailed,
    ScanResult,
    _NoMatches,
    parse_airport_list,
    parse_stay_range,
    summarize,
)

logger = logging.getLogger(__name__)

HUB_CITIES: dict[str, dict] = {
    "SAO": {
        "label": "São Paulo",
        "positioning": ("GRU", "CGH", "VCP"),
        "longhaul": ("GRU", "VCP"),
    },
    "RIO": {
        "label": "Rio",
        "positioning": ("GIG", "SDU"),
        "longhaul": ("GIG",),
    },
}

_AIRPORT_CITY: dict[str, str] = {
    code: city for city, meta in HUB_CITIES.items() for code in meta["positioning"]
}

LEG_OPTIONS_PER_DATE = 5


def city_of(airport: str) -> str | None:
    return _AIRPORT_CITY.get(airport.upper())


def city_label(city: str) -> str:
    meta = HUB_CITIES.get(city)
    return meta["label"] if meta else city


def _city_airports(city: str, kind: str) -> tuple[str, ...]:
    meta = HUB_CITIES.get(city)
    return meta[kind] if meta else ()


@dataclass(frozen=True)
class LegQuote:
    date: str
    price: float
    from_airport: str
    to_airport: str
    departure_at: datetime | None = None
    arrival_at: datetime | None = None
    airline: str | None = None
    booking_url: str | None = None

    @classmethod
    def from_quote(cls, quote: Quote) -> LegQuote:
        return cls(
            date=quote.date,
            price=quote.price,
            from_airport=quote.from_airport,
            to_airport=quote.to_airport,
            departure_at=quote.departure_at,
            arrival_at=quote.arrival_at,
            airline=quote.airline,
            booking_url=quote.booking_url,
        )

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "price": self.price,
            "from_airport": self.from_airport,
            "to_airport": self.to_airport,
            "airline": self.airline,
            "booking_url": self.booking_url,
            "departure": self.departure_at.strftime("%I:%M %p") if self.departure_at else None,
        }


@dataclass
class HubCombo:
    price: float
    nights_out: int
    nights_back: int
    dest_stay: int
    pos_out: LegQuote
    long_out: LegQuote
    long_in: LegQuote
    pos_in: LegQuote

    @property
    def hub_out_city(self) -> str | None:
        return city_of(self.pos_out.to_airport)

    @property
    def hub_back_city(self) -> str | None:
        return city_of(self.long_in.to_airport)

    def to_day(self) -> dict:
        legs = [self.pos_out, self.long_out, self.long_in, self.pos_in]
        return {
            "date": self.pos_out.date,
            "return_date": self.pos_in.date,
            "price": self.price,
            "from_airport": self.pos_out.from_airport,
            "to_airport": self.long_out.to_airport,
            "airline": self.long_out.airline,
            "fare_type": "via_hub",
            "hub_out": self.hub_out_city,
            "hub_back": self.hub_back_city,
            "nights_out": self.nights_out,
            "nights_back": self.nights_back,
            "dest_stay": self.dest_stay,
            "legs": [leg.to_dict() for leg in legs],
        }


def connection_ok(
    arrival: datetime | None,
    departure: datetime | None,
    nights: int,
    *,
    min_hours: float = MIN_SELF_TRANSFER_HOURS,
) -> bool:
    """0 nights needs a proven self-transfer gap. Overnight stays do not."""
    if nights >= 1:
        return True
    if arrival is None or departure is None:
        return False
    return departure.replace(tzinfo=None) - arrival.replace(tzinfo=None) >= timedelta(hours=min_hours)


def _index(quotes: list[LegQuote]) -> dict[tuple, list[LegQuote]]:
    indexed: dict[tuple, list[LegQuote]] = {}
    for quote in quotes:
        indexed.setdefault((quote.from_airport, quote.date), []).append(quote)
    return indexed


@dataclass
class ViaRequest:
    origins: list[str]
    destinations: list[str]
    stay_min: int
    stay_max: int
    hub_nights_min: int
    hub_nights_max: int
    save: bool = False


def parse_via_args(args: list[str]) -> ViaRequest:
    """Parse `/via ORIGIN DEST STAY [HUB_NIGHTS] [save]`. Raises ValueError(code)."""
    tokens = list(args)
    save = False
    if tokens and tokens[-1].lower() == "save":
        save = True
        tokens = tokens[:-1]
    if len(tokens) not in (3, 4):
        raise ValueError("usage")
    origins = parse_airport_list(tokens[0])
    destinations = parse_airport_list(tokens[1])
    if not origins or not destinations:
        raise ValueError("airports")
    for code in origins + destinations:
        if len(code) != 3 or not code.isalpha():
            raise ValueError("airports")
    try:
        stay_min, stay_max = parse_stay_range(tokens[2])
    except ValueError as exc:
        raise ValueError("stay") from exc
    if stay_min is None or stay_min < MIN_STAY_DAYS or stay_max > MAX_STAY_DAYS:
        raise ValueError("stay")
    if len(tokens) == 4:
        try:
            hub_min, hub_max = parse_stay_range(tokens[3])
        except ValueError as exc:
            raise ValueError("hub") from exc
        if hub_min is None or hub_min < 0 or hub_max > MAX_HUB_NIGHTS:
            raise ValueError("hub")
    else:
        hub_min, hub_max = 0, 0
    return ViaRequest(origins, destinations, stay_min, stay_max, hub_min, hub_max, save)


def join_hub_itineraries(
    pos_out: list[LegQuote],
    long_out: list[LegQuote],
    long_in: list[LegQuote],
    pos_in: list[LegQuote],
    *,
    dest_stay_min: int,
    dest_stay_max: int,
    hub_nights_min: int,
    hub_nights_max: int,
    min_transfer_hours: float = MIN_SELF_TRANSFER_HOURS,
    limit: int = 50,
) -> list[HubCombo]:
    """Match four one-way leg lists. Outbound and return pick hub and nights independently."""
    long_out_idx = _index(long_out)
    long_in_idx = _index(long_in)
    pos_in_idx = _index(pos_in)
    hub_nights = range(hub_nights_min, hub_nights_max + 1)
    dest_stays = range(dest_stay_min, dest_stay_max + 1)
    found: dict[tuple, HubCombo] = {}

    for outbound_pos in pos_out:
        city = city_of(outbound_pos.to_airport)
        if city is None:
            continue
        for nights_out in hub_nights:
            if nights_out == 0:
                if outbound_pos.to_airport not in _city_airports(city, "longhaul"):
                    continue
                depart_airports: tuple[str, ...] = (outbound_pos.to_airport,)
            else:
                depart_airports = _city_airports(city, "longhaul")
            long_date = add_days(outbound_pos.date, nights_out)
            for depart_airport in depart_airports:
                for outbound_long in long_out_idx.get((depart_airport, long_date), []):
                    if not connection_ok(
                        outbound_pos.arrival_at,
                        outbound_long.departure_at,
                        nights_out,
                        min_hours=min_transfer_hours,
                    ):
                        continue
                    for stay in dest_stays:
                        ret_date = add_days(long_date, stay)
                        for inbound_long in long_in_idx.get((outbound_long.to_airport, ret_date), []):
                            back_city = city_of(inbound_long.to_airport)
                            if back_city is None:
                                continue
                            if inbound_long.to_airport not in _city_airports(back_city, "longhaul"):
                                continue
                            for nights_back in hub_nights:
                                if nights_back == 0:
                                    home_airports: tuple[str, ...] = (inbound_long.to_airport,)
                                else:
                                    home_airports = _city_airports(back_city, "positioning")
                                home_date = add_days(ret_date, nights_back)
                                for home_airport in home_airports:
                                    for inbound_pos in pos_in_idx.get((home_airport, home_date), []):
                                        if not connection_ok(
                                            inbound_long.arrival_at,
                                            inbound_pos.departure_at,
                                            nights_back,
                                            min_hours=min_transfer_hours,
                                        ):
                                            continue
                                        combo = HubCombo(
                                            price=outbound_pos.price
                                            + outbound_long.price
                                            + inbound_long.price
                                            + inbound_pos.price,
                                            nights_out=nights_out,
                                            nights_back=nights_back,
                                            dest_stay=stay,
                                            pos_out=outbound_pos,
                                            long_out=outbound_long,
                                            long_in=inbound_long,
                                            pos_in=inbound_pos,
                                        )
                                        key = tuple(
                                            (leg.date, leg.from_airport, leg.to_airport)
                                            for leg in (outbound_pos, outbound_long, inbound_long, inbound_pos)
                                        )
                                        existing = found.get(key)
                                        if existing is None or combo.price < existing.price:
                                            found[key] = combo

    return sorted(found.values(), key=lambda c: c.price)[:limit]


async def _run_limited(planner: Planner, probes: list[Probe], limit: int):
    """Run probes with at most `limit` network requests, charged to the planner's main budget."""
    main = planner.budget
    sub = ScanBudget(max(0, min(limit, main.remaining)))
    planner.budget = sub
    try:
        await planner.run_all(probes)
    finally:
        main.used += sub.used
        planner.budget = main
        planner.exhausted = main.remaining <= 0


def _legs(planner: Planner, probes: list[Probe]) -> list[LegQuote]:
    legs = []
    for probe in probes:
        for quote in planner.options.get(probe, [])[:LEG_OPTIONS_PER_DATE]:
            legs.append(LegQuote.from_quote(quote))
    return legs


def _nights_sample(lo: int, hi: int) -> list[int]:
    return sorted({lo, hi, (lo + hi) // 2})


async def scan_via_route(
    fares: FareService,
    *,
    origins: list[str],
    destinations: list[str],
    stay_min: int,
    stay_max: int | None = None,
    hub_nights_min: int = 0,
    hub_nights_max: int | None = None,
    max_stops: str = "any",
    currency: str,
    days: int = DAYS_TO_SCAN,
    budget_limit: int = VIA_BUDGET_PER_SCAN,
    hubs: list[str] | None = None,
    today: date | None = None,
) -> ScanResult | _NoMatches:
    stay_max = stay_max or stay_min
    hub_nights_max = hub_nights_min if hub_nights_max is None else hub_nights_max
    longhaul = [h for h in (hubs or VIA_LONGHAUL_HUBS) if city_of(h) and h in _city_airports(city_of(h), "longhaul")]
    origins = [o for o in origins if city_of(o) is None] or origins
    start = (today or date.today()) + timedelta(days=1)
    dates = window_dates(start, days)

    budget = ScanBudget(budget_limit)
    planner = Planner(fares, budget=budget, max_stops=max_stops, currency=currency)

    # 1) Long-haul outbound dominates the price: sample it first.
    long_pairs = [(h, d) for h in longhaul for d in destinations][:MAX_AIRPORT_COMBOS]
    long_out_probes = coarse_probes(
        long_pairs, dates, [None], budget=max(1, int(budget_limit * 0.35)), base_step=COARSE_STEP_DAYS
    )
    await _run_limited(planner, long_out_probes, int(budget_limit * 0.35))
    long_out_set = set(long_out_probes)
    best_out = [p for p, _ in planner.ranked(round_trip=False) if p in long_out_set][:3]

    # 2) Long-haul return for the best outbound dates; same hub first, then the other city.
    long_in_probes: list[Probe] = []
    for probe in best_out:
        hubs_back = [probe.from_airport] + [h for h in longhaul if h != probe.from_airport]
        for hub in hubs_back:
            for stay in sample_stays(stay_min, stay_max):
                cand = Probe(probe.to_airport, hub, add_days(probe.date, stay))
                if cand not in long_in_probes:
                    long_in_probes.append(cand)
    await _run_limited(planner, long_in_probes, int(budget_limit * 0.3))

    # 3) Positioning legs only around the cheapest long-haul pairs.
    long_out_legs = _legs(planner, long_out_probes)
    long_in_legs = _legs(planner, long_in_probes)
    pairs: list[tuple[float, Probe, Probe]] = []
    for out_probe in best_out:
        for in_probe in long_in_probes:
            if not planner.options.get(in_probe) or in_probe.from_airport != out_probe.to_airport:
                continue
            if stay_min <= stay_length(out_probe.date, in_probe.date) <= stay_max:
                total = planner.options[out_probe][0].price + planner.options[in_probe][0].price
                pairs.append((total, out_probe, in_probe))
    pairs = sorted(pairs, key=lambda item: item[0])[:3]

    pos_out_probes: list[Probe] = []
    pos_in_probes: list[Probe] = []
    nights = _nights_sample(hub_nights_min, hub_nights_max)
    for _, out_probe, in_probe in pairs:
        out_city = city_of(out_probe.from_airport)
        back_city = city_of(in_probe.to_airport)
        for n in nights:
            out_airports = (out_probe.from_airport,) if n == 0 else _city_airports(out_city, "positioning")
            back_airports = (in_probe.to_airport,) if n == 0 else _city_airports(back_city, "positioning")
            for origin in origins[:2]:
                for airport in out_airports:
                    cand = Probe(origin, airport, add_days(out_probe.date, -n))
                    if cand.date >= dates[0] and cand not in pos_out_probes:
                        pos_out_probes.append(cand)
                for airport in back_airports:
                    cand = Probe(airport, origin, add_days(in_probe.date, n))
                    if cand not in pos_in_probes:
                        pos_in_probes.append(cand)
    # Interleave so a short budget still gets both directions for the best pair.
    interleaved = [p for pair in zip(pos_out_probes, pos_in_probes) for p in pair]
    longest = pos_out_probes if len(pos_out_probes) > len(pos_in_probes) else pos_in_probes
    interleaved += longest[min(len(pos_out_probes), len(pos_in_probes)):]
    await _run_limited(planner, interleaved, max(0, budget.remaining - 1))

    combos = join_hub_itineraries(
        _legs(planner, pos_out_probes),
        long_out_legs,
        long_in_legs,
        _legs(planner, pos_in_probes),
        dest_stay_min=stay_min,
        dest_stay_max=stay_max,
        hub_nights_min=hub_nights_min,
        hub_nights_max=hub_nights_max,
    )
    if not combos:
        if planner.stopped is not None:
            raise ScanDeferred(planner.stopped.reason, planner.stopped.retry_after, budget.used)
        if planner.failures and planner.failures >= len(planner.options):
            raise ScanFailed(f"{planner.failures} request(s) failed")
        return NO_MATCHES

    best = combos[0]
    direct_price = None
    if not planner.stopped:
        planner.exhausted = budget.remaining <= 0
        direct = await planner.cheapest(
            Probe(best.pos_out.from_airport, best.long_out.to_airport, best.pos_out.date, best.pos_in.date)
        )
        direct_price = direct.price if direct else None

    days_list = [c.to_day() for c in combos]
    avg, low, high = summarize(days_list)
    top = days_list[:TOP_CHEAPEST]
    return ScanResult(
        from_airport=",".join(origins),
        to_airport=",".join(destinations),
        from_airports=origins,
        to_airports=destinations,
        currency=currency,
        provider=fares.name,
        fare_type="via_hub",
        top_days=top,
        via_combos=top,
        avg_price=avg,
        min_price=low,
        max_price=high,
        stay_days=stay_min,
        stay_days_max=stay_max,
        hub_nights=hub_nights_min,
        hub_nights_max=hub_nights_max,
        direct_price=direct_price,
        requests_used=budget.used,
        probes_checked=len(planner.options),
        partial=planner.stopped is not None,
        partial_reason=planner.stopped.reason if planner.stopped else None,
    )

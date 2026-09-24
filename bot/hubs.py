"""Pure join of separate tickets through São Paulo or Rio hubs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from bot.config import MAX_HUB_NIGHTS, MAX_STAY_DAYS, MIN_SELF_TRANSFER_HOURS, MIN_STAY_DAYS

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

_AIRPORT_CITY: dict[str, str] = {}
for _city, _meta in HUB_CITIES.items():
    for _code in _meta["positioning"]:
        _AIRPORT_CITY[_code] = _city


def city_of(airport: str) -> str | None:
    return _AIRPORT_CITY.get(airport.upper())


def city_label(city: str) -> str:
    meta = HUB_CITIES.get(city)
    return meta["label"] if meta else city


def positioning_airports() -> list[str]:
    codes: list[str] = []
    for meta in HUB_CITIES.values():
        codes.extend(meta["positioning"])
    return codes


def longhaul_airports() -> list[str]:
    codes: list[str] = []
    for meta in HUB_CITIES.values():
        codes.extend(meta["longhaul"])
    return codes


def _city_airports(city: str, kind: str) -> tuple[str, ...]:
    meta = HUB_CITIES.get(city)
    if not meta:
        return ()
    return meta[kind]


@dataclass(frozen=True)
class LegQuote:
    date: str
    price: float
    from_airport: str
    to_airport: str


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


def add_days(date: str, days: int) -> str:
    return (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")


def airports_compatible(arrive_airport: str, depart_airport: str, nights: int) -> bool:
    """Same-day transfers stay on one long-haul airport. Overnight stays may change airport in the city."""
    arrive = arrive_airport.upper()
    depart = depart_airport.upper()
    city = city_of(arrive)
    if city is None or city != city_of(depart):
        return False
    if nights <= 0:
        return arrive == depart and arrive in _city_airports(city, "longhaul")
    return True


def connection_ok(
    arrival,
    departure,
    nights: int,
    *,
    min_hours: float = MIN_SELF_TRANSFER_HOURS,
) -> bool:
    """0 nights needs a proven self-transfer gap. Overnight stays do not."""
    if nights >= 1:
        return True
    if arrival is None or departure is None:
        return False
    arrive_at = _naive(arrival)
    depart_at = _naive(departure)
    return depart_at - arrive_at >= timedelta(hours=min_hours)


def _naive(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


def _index(quotes: list[LegQuote], key_fn) -> dict[tuple, list[LegQuote]]:
    indexed: dict[tuple, list[LegQuote]] = {}
    for quote in quotes:
        indexed.setdefault(key_fn(quote), []).append(quote)
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
    """Parse `/via ORIGIN DEST STAY [HUB_NIGHTS] [save]`."""
    from bot.scanner import parse_airport_list, parse_stay_range

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
    return ViaRequest(
        origins=origins,
        destinations=destinations,
        stay_min=stay_min,
        stay_max=stay_max,
        hub_nights_min=hub_min,
        hub_nights_max=hub_max,
        save=save,
    )


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
    limit: int = 50,
) -> list[HubCombo]:
    """Match four one-way calendars. Ida and volta pick hub and nights independently."""
    long_out_idx = _index(long_out, lambda q: (q.from_airport, q.date))
    long_in_idx = _index(long_in, lambda q: (q.from_airport, q.date))
    pos_in_idx = _index(pos_in, lambda q: (q.from_airport, q.date))

    hub_nights = range(hub_nights_min, hub_nights_max + 1)
    dest_stays = range(dest_stay_min, dest_stay_max + 1)
    found: dict[tuple, HubCombo] = {}

    for outbound_pos in pos_out:
        city = city_of(outbound_pos.to_airport)
        if city is None:
            continue
        for nights_out in hub_nights:
            if nights_out == 0:
                depart_airports = (outbound_pos.to_airport,)
                if outbound_pos.to_airport not in _city_airports(city, "longhaul"):
                    continue
            else:
                depart_airports = _city_airports(city, "longhaul")
            long_date = add_days(outbound_pos.date, nights_out)
            for depart_airport in depart_airports:
                for outbound_long in long_out_idx.get((depart_airport, long_date), []):
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
                                    home_airports = (inbound_long.to_airport,)
                                else:
                                    home_airports = _city_airports(back_city, "positioning")
                                home_date = add_days(ret_date, nights_back)
                                for home_airport in home_airports:
                                    for inbound_pos in pos_in_idx.get((home_airport, home_date), []):
                                        total = (
                                            outbound_pos.price
                                            + outbound_long.price
                                            + inbound_long.price
                                            + inbound_pos.price
                                        )
                                        combo = HubCombo(
                                            price=total,
                                            nights_out=nights_out,
                                            nights_back=nights_back,
                                            dest_stay=stay,
                                            pos_out=outbound_pos,
                                            long_out=outbound_long,
                                            long_in=inbound_long,
                                            pos_in=inbound_pos,
                                        )
                                        key = (
                                            outbound_pos.date,
                                            outbound_pos.from_airport,
                                            outbound_pos.to_airport,
                                            outbound_long.date,
                                            outbound_long.from_airport,
                                            outbound_long.to_airport,
                                            inbound_long.date,
                                            inbound_long.from_airport,
                                            inbound_long.to_airport,
                                            inbound_pos.date,
                                            inbound_pos.from_airport,
                                            inbound_pos.to_airport,
                                        )
                                        existing = found.get(key)
                                        if existing is None or combo.price < existing.price:
                                            found[key] = combo

    ranked = sorted(found.values(), key=lambda c: c.price)
    return ranked[:limit]

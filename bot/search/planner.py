"""Decide which dates to ask Google about, within a request budget.

fast-flights has no calendar endpoint: each request prices one date (or one date pair).
So we sample a coarse grid first, then spend the rest of the budget around the cheapest hits.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from bot.fares.models import BudgetExhausted, Quote, SearchFailed, SearchUnavailable
from bot.fares.service import FareService, ScanBudget

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Probe:
    from_airport: str
    to_airport: str
    date: str
    return_date: str | None = None

    @property
    def stay(self) -> int | None:
        if not self.return_date:
            return None
        return stay_length(self.date, self.return_date)


def add_days(value: str, days: int) -> str:
    return (datetime.strptime(value, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")


def stay_length(outbound: str, inbound: str) -> int:
    return (datetime.strptime(inbound, "%Y-%m-%d") - datetime.strptime(outbound, "%Y-%m-%d")).days


def window_dates(start: date, days: int) -> list[str]:
    return [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]


def sample_stays(stay_min: int, stay_max: int | None) -> list[int]:
    stay_max = stay_max or stay_min
    values = [stay_min, stay_max, (stay_min + stay_max) // 2]
    return sorted(set(values))


def coarse_probes(
    pairs: list[tuple[str, str]],
    dates: list[str],
    stays: list[int | None],
    *,
    budget: int,
    base_step: int,
) -> list[Probe]:
    """Evenly spaced grid that fits the budget. Date-major order so an early stop still covers every pair."""
    combos = [(f, t, s) for f, t in pairs for s in stays]
    if not combos or not dates or budget <= 0:
        return []
    if len(combos) > budget:
        combos = combos[:budget]
        indexes = [len(dates) // 2]
    else:
        per_combo = max(1, budget // len(combos))
        step = max(base_step, math.ceil(len(dates) / per_combo))
        indexes = list(range(min(step // 2, len(dates) - 1), len(dates), step))[:per_combo]
    probes = []
    for idx in indexes:
        for f, t, stay in combos:
            d = dates[idx]
            probes.append(Probe(f, t, d, add_days(d, stay) if stay else None))
    return probes


def neighbor_probes(
    probe: Probe,
    *,
    distance: int,
    window: set[str],
    stay_min: int | None,
    stay_max: int | None,
) -> list[Probe]:
    """Probes exactly `distance` days away: shift the trip, or (round trip) stretch/shrink the stay."""
    out: list[Probe] = []
    for delta in (-distance, distance):
        d = add_days(probe.date, delta)
        if d not in window:
            continue
        ret = add_days(probe.return_date, delta) if probe.return_date else None
        out.append(Probe(probe.from_airport, probe.to_airport, d, ret))
    if probe.return_date and stay_min is not None:
        hi = stay_max or stay_min
        for delta in (-distance, distance):
            stay = probe.stay + delta
            if stay_min <= stay <= hi:
                out.append(
                    Probe(probe.from_airport, probe.to_airport, probe.date, add_days(probe.date, stay))
                )
    return out


class Planner:
    def __init__(self, fares: FareService, *, budget: ScanBudget, max_stops: str, currency: str):
        self.fares = fares
        self.budget = budget
        self.max_stops = max_stops
        self.currency = currency
        self.options: dict[Probe, list[Quote]] = {}
        self.stopped: SearchUnavailable | None = None
        self.exhausted = False
        self.failures = 0

    @property
    def halted(self) -> bool:
        return self.stopped is not None or self.exhausted

    def tried(self, probe: Probe) -> bool:
        return probe in self.options

    async def fetch(self, probe: Probe) -> list[Quote]:
        if probe in self.options:
            return self.options[probe]
        if self.halted:
            return []
        try:
            quotes = await self.fares.search(
                probe.from_airport,
                probe.to_airport,
                probe.date,
                probe.return_date,
                max_stops=self.max_stops,
                currency=self.currency,
                budget=self.budget,
            )
        except BudgetExhausted:
            self.exhausted = True
            return []
        except SearchUnavailable as exc:
            logger.warning("Stopping scan: %s", exc)
            self.stopped = exc
            return []
        except SearchFailed as exc:
            self.failures += 1
            logger.warning("Search failed for %s: %s", probe, exc)
            quotes = []
        self.options[probe] = quotes
        return quotes

    async def cheapest(self, probe: Probe) -> Quote | None:
        quotes = await self.fetch(probe)
        return quotes[0] if quotes else None

    async def run_all(self, probes: list[Probe]):
        for probe in probes:
            if self.halted:
                return
            await self.fetch(probe)

    def ranked(self, *, round_trip: bool | None = None) -> list[tuple[Probe, Quote]]:
        found = [
            (p, q[0])
            for p, q in self.options.items()
            if q and (round_trip is None or bool(p.return_date) == round_trip)
        ]
        return sorted(found, key=lambda item: item[1].price)

    async def refine(
        self,
        *,
        window: set[str],
        stay_min: int | None,
        stay_max: int | None,
        top_k: int,
        radius: int,
        round_trip: bool,
    ):
        """Walk outward from the current best probes, nearest days first, until budget or no news."""
        for _ in range(2):
            best_before = self.ranked(round_trip=round_trip)[:1]
            top = [p for p, _ in self.ranked(round_trip=round_trip)[:top_k]]
            queue: list[Probe] = []
            for distance in range(1, radius + 1):
                for probe in top:
                    for cand in neighbor_probes(
                        probe, distance=distance, window=window, stay_min=stay_min, stay_max=stay_max
                    ):
                        if not self.tried(cand) and cand not in queue:
                            queue.append(cand)
            if not queue:
                return
            await self.run_all(queue)
            if self.halted or self.ranked(round_trip=round_trip)[:1] == best_before:
                return

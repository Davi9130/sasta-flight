from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime


@dataclass
class Quote:
    """One bookable itinerary option. For round trips, times describe the outbound leg."""

    from_airport: str
    to_airport: str
    date: str
    return_date: str | None
    price: float
    currency: str
    airlines: list[str] = field(default_factory=list)
    departure_at: datetime | None = None
    arrival_at: datetime | None = None
    duration: int | None = None
    stops: int = 0
    booking_url: str | None = None

    @property
    def airline(self) -> str | None:
        return ", ".join(self.airlines[:2]) if self.airlines else None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["departure_at"] = self.departure_at.isoformat() if self.departure_at else None
        data["arrival_at"] = self.arrival_at.isoformat() if self.arrival_at else None
        return data

    @classmethod
    def from_dict(cls, data: dict) -> Quote:
        values = dict(data)
        for key in ("departure_at", "arrival_at"):
            if values.get(key):
                values[key] = datetime.fromisoformat(values[key])
        return cls(**values)


class SearchUnavailable(Exception):
    """Google is off-limits right now (block, cooldown or global budget)."""

    def __init__(self, reason: str, retry_after: float):
        super().__init__(f"{reason} (retry in {retry_after:.0f}s)")
        self.reason = reason
        self.retry_after = max(0.0, retry_after)


class BudgetExhausted(Exception):
    """The per-scan request budget is spent; stop probing and use what we have."""


class SearchFailed(Exception):
    """A single search failed for a non-block reason (network, timeout, parse)."""

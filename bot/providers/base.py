from typing import Protocol

from fli.models import DateSearchFilters, FlightSearchFilters


class FareProvider(Protocol):
    """Abstract fare data source. Implementations wrap Google Flights or future APIs."""

    name: str

    def search_dates(
        self,
        filters: DateSearchFilters,
        *,
        currency: str,
        country: str,
    ) -> list:
        """Return calendar price results for a date range."""
        ...

    def search_flights(
        self,
        filters: FlightSearchFilters,
        *,
        currency: str,
        country: str,
    ) -> list:
        """Return detailed flight offers for a specific itinerary."""
        ...

from fli.models import DateSearchFilters, FlightSearchFilters
from fli.search import SearchDates, SearchFlights


class FliProvider:
    """Google Flights via the unofficial fli library (no API key)."""

    name = "fli"

    def search_dates(
        self,
        filters: DateSearchFilters,
        *,
        currency: str,
        country: str,
    ) -> list:
        return SearchDates().search(filters, currency=currency, country=country)

    def search_flights(
        self,
        filters: FlightSearchFilters,
        *,
        currency: str,
        country: str,
    ) -> list:
        return SearchFlights().search(filters, currency=currency, country=country)

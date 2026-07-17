from bot.config import FARE_PROVIDER
from bot.providers.base import FareProvider
from bot.providers.fli_provider import FliProvider


def get_fare_provider(name: str | None = None) -> FareProvider:
    """Return the configured fare provider. Extension point for future providers."""
    provider_name = (name or FARE_PROVIDER).lower()
    if provider_name == "fli":
        return FliProvider()
    raise ValueError(f"Unknown fare provider: {provider_name}")


__all__ = ["FareProvider", "FliProvider", "get_fare_provider"]

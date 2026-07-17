"""Exchange rates via Frankfurter (no API key), with DB-backed daily cache."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import httpx

from bot.config import CURRENCY, DISPLAY_CURRENCIES, FX_API_URL, FX_CACHE_HOURS

if TYPE_CHECKING:
    from bot.db import Database

logger = logging.getLogger(__name__)


class FxService:
    def __init__(self, db: Database | None = None, api_url: str = FX_API_URL):
        self.db = db
        self.api_url = api_url.rstrip("/")

    async def get_rates(
        self,
        base: str = CURRENCY,
        quotes: tuple[str, ...] | list[str] | None = None,
    ) -> dict[str, float]:
        """Return {quote: rate} for converting 1 base → quote amount."""
        quotes = tuple(q for q in (quotes or DISPLAY_CURRENCIES) if q != base)
        rates: dict[str, float] = {}
        missing: list[str] = []

        for quote in quotes:
            cached = await self._get_cached(base, quote)
            if cached is not None:
                rates[quote] = cached
            else:
                missing.append(quote)

        if missing:
            fetched = await self._fetch_rates(base, missing)
            for quote, rate in fetched.items():
                rates[quote] = rate
                await self._store(base, quote, rate)

        # Fill any still-missing quotes from stale DB cache if possible
        for quote in quotes:
            if quote in rates:
                continue
            stale = await self._get_cached(base, quote, allow_stale=True)
            if stale is not None:
                logger.warning("Using stale FX rate %s/%s=%s", base, quote, stale)
                rates[quote] = stale

        return rates

    async def convert(
        self,
        amount: float,
        base: str = CURRENCY,
        quotes: tuple[str, ...] | list[str] | None = None,
    ) -> dict[str, float]:
        rates = await self.get_rates(base=base, quotes=quotes)
        return {quote: amount * rate for quote, rate in rates.items()}

    async def _get_cached(
        self, base: str, quote: str, allow_stale: bool = False
    ) -> float | None:
        if not self.db:
            return None
        row = await self.db.get_fx_rate(base, quote)
        if not row:
            return None
        if allow_stale:
            return float(row["rate"])
        fetched_at = row.get("fetched_at")
        if not fetched_at:
            return float(row["rate"])
        try:
            ts = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
            if age_hours <= FX_CACHE_HOURS:
                return float(row["rate"])
        except ValueError:
            return float(row["rate"])
        return None

    async def _store(self, base: str, quote: str, rate: float, source: str = "frankfurter"):
        if not self.db:
            return
        await self.db.save_fx_rate(
            base=base,
            quote=quote,
            rate=rate,
            source=source,
            as_of_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        )

    async def _fetch_rates(self, base: str, quotes: list[str]) -> dict[str, float]:
        if not quotes:
            return {}
        params = {"base": base, "quotes": ",".join(quotes)}
        # Prefer BCB for BRL pairs when available
        if base == "BRL" or "BRL" in quotes:
            params["providers"] = "BCB,ECB"
        url = f"{self.api_url}/rates"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            logger.exception("Failed to fetch FX rates from %s", url)
            # Try single-pair fallbacks
            result = {}
            for quote in quotes:
                rate = await self._fetch_single(base, quote)
                if rate is not None:
                    result[quote] = rate
            return result

        rates = {}
        # v2 may return list of rows or dict
        if isinstance(data, dict) and "rates" in data:
            # v1-like shape
            for quote, rate in data["rates"].items():
                if quote in quotes:
                    rates[quote] = float(rate)
        elif isinstance(data, list):
            for row in data:
                quote = row.get("quote") or row.get("currency")
                rate = row.get("rate")
                if quote in quotes and rate is not None:
                    rates[quote] = float(rate)
        elif isinstance(data, dict):
            # map of quote -> rate
            for quote in quotes:
                if quote in data and isinstance(data[quote], (int, float)):
                    rates[quote] = float(data[quote])
                elif quote in data and isinstance(data[quote], dict) and "rate" in data[quote]:
                    rates[quote] = float(data[quote]["rate"])
        return rates

    async def _fetch_single(self, base: str, quote: str) -> float | None:
        url = f"{self.api_url}/rate/{base}/{quote}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
            if isinstance(data, dict) and "rate" in data:
                return float(data["rate"])
        except Exception:
            logger.exception("Failed single FX fetch %s/%s", base, quote)
        return None

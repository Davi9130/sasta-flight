"""Approximate EUR/USD equivalents via Frankfurter (no API key), cached in SQLite."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from bot.config import CURRENCY, DISPLAY_CURRENCIES, FX_API_URL, FX_CACHE_HOURS

logger = logging.getLogger(__name__)


def _age_hours(fetched_at: str) -> float:
    ts = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 3600


def _extract_rates(data, quotes: list[str]) -> dict[str, float]:
    """Frankfurter v1 returns {"rates": {...}}; v2 returns a list of rows."""
    if isinstance(data, dict) and isinstance(data.get("rates"), dict):
        return {q: float(r) for q, r in data["rates"].items() if q in quotes}
    if isinstance(data, list):
        out = {}
        for row in data:
            quote = row.get("quote") or row.get("currency")
            if quote in quotes and row.get("rate") is not None:
                out[quote] = float(row["rate"])
        return out
    return {}


class FxService:
    def __init__(self, db=None, api_url: str = FX_API_URL, http: httpx.AsyncClient | None = None):
        self.db = db
        self.api_url = api_url.rstrip("/")
        self.http = http

    async def get_rates(self, base: str = CURRENCY, quotes=None) -> dict[str, float]:
        wanted = [q for q in (quotes or DISPLAY_CURRENCIES) if q != base]
        rates: dict[str, float] = {}
        stale: dict[str, float] = {}
        for quote in wanted:
            row = await self.db.get_fx_rate(base, quote) if self.db else None
            if not row:
                continue
            try:
                fresh = _age_hours(row["fetched_at"]) <= FX_CACHE_HOURS
            except (ValueError, TypeError):
                fresh = False
            (rates if fresh else stale)[quote] = float(row["rate"])

        missing = [q for q in wanted if q not in rates]
        if missing:
            fetched = await self._fetch(base, missing)
            for quote, rate in fetched.items():
                rates[quote] = rate
                if self.db:
                    await self.db.save_fx_rate(base, quote, rate, source="frankfurter")
        for quote, rate in stale.items():
            if quote not in rates:
                logger.warning("Using stale FX rate %s/%s=%s", base, quote, rate)
                rates[quote] = rate
        return rates

    async def convert(self, amount: float, base: str = CURRENCY, quotes=None) -> dict[str, float]:
        rates = await self.get_rates(base=base, quotes=quotes)
        return {quote: amount * rate for quote, rate in rates.items()}

    async def _fetch(self, base: str, quotes: list[str]) -> dict[str, float]:
        params = {"base": base, "quotes": ",".join(quotes)}
        if base == "BRL" or "BRL" in quotes:
            params["providers"] = "BCB,ECB"
        url = f"{self.api_url}/rates"
        try:
            if self.http is not None:
                resp = await self.http.get(url, params=params)
            else:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(url, params=params)
            resp.raise_for_status()
            return _extract_rates(resp.json(), quotes)
        except Exception:
            logger.exception("Failed to fetch FX rates from %s", url)
            return {}

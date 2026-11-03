"""Cache -> guard -> client. The only way the rest of the bot talks to Google."""

from __future__ import annotations

import asyncio
import logging

from bot.config import REQ_ERROR_RETRIES, REQ_TIMEOUT_SECS
from bot.fares.cache import QuoteCache, cache_key
from bot.fares.client import BLOCKED, ERROR, FareClient
from bot.fares.guard import Guard
from bot.fares.models import BudgetExhausted, Quote, SearchFailed, SearchUnavailable

logger = logging.getLogger(__name__)


class ScanBudget:
    """Network requests one scan may spend. Cache hits are free."""

    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0

    @property
    def remaining(self) -> int:
        return self.limit - self.used

    def spend(self):
        if self.remaining <= 0:
            raise BudgetExhausted()
        self.used += 1


class FareService:
    def __init__(
        self,
        client: FareClient,
        guard: Guard,
        cache: QuoteCache,
        *,
        error_retries: int = REQ_ERROR_RETRIES,
        timeout: float = REQ_TIMEOUT_SECS,
    ):
        self.client = client
        self.guard = guard
        self.cache = cache
        self.error_retries = error_retries
        self.timeout = timeout

    @property
    def name(self) -> str:
        return self.client.name

    async def search(
        self,
        from_airport: str,
        to_airport: str,
        date: str,
        return_date: str | None = None,
        *,
        max_stops: str = "any",
        currency: str,
        budget: ScanBudget | None = None,
    ) -> list[Quote]:
        """All options for one date (cheapest first).

        Raises BudgetExhausted, SearchUnavailable (stop scanning) or SearchFailed (skip this date).
        """
        key = cache_key(from_airport, to_airport, date, return_date, max_stops, currency)
        cached = await self.cache.get(key)
        if cached is not None:
            return cached

        block_attempts = max(1, len(self.guard.proxies))
        blocks = errors = 0
        async with self.guard.lock:
            while True:
                if budget is not None and budget.remaining <= 0:
                    raise BudgetExhausted()
                await self.guard.check()
                proxy = await self.guard.pick_proxy()
                await self.guard.pace()
                if budget is not None:
                    budget.spend()
                try:
                    result = await asyncio.wait_for(
                        asyncio.to_thread(
                            self.client.fetch,
                            from_airport,
                            to_airport,
                            date,
                            return_date,
                            max_stops=max_stops,
                            currency=currency,
                            proxy=proxy,
                        ),
                        timeout=self.timeout + 15,
                    )
                except asyncio.TimeoutError:
                    result = None
                status = result.status if result else ERROR
                detail = result.detail if result else "timeout"
                await self.guard.record(status, proxy, detail)
                logger.info(
                    "search %s->%s %s%s stops=%s -> %s%s (%d options)",
                    from_airport,
                    to_airport,
                    date,
                    f"/{return_date}" if return_date else "",
                    max_stops,
                    status,
                    f" [{detail}]" if detail else "",
                    len(result.quotes) if result else 0,
                )

                if status == BLOCKED:
                    self.client.reset(proxy)
                    blocks += 1
                    if proxy is not None and blocks < block_attempts:
                        continue
                    raise await self._unavailable(detail or "blocked")
                if status == ERROR:
                    errors += 1
                    if errors <= self.error_retries:
                        continue
                    raise SearchFailed(detail or "error")

                await self.cache.put(key, result.quotes)
                return result.quotes

    async def _unavailable(self, detail: str) -> SearchUnavailable:
        reason, remaining = await self.guard.circuit()
        if reason:
            return SearchUnavailable(reason, remaining)
        try:
            await self.guard.pick_proxy()
        except SearchUnavailable as exc:
            return SearchUnavailable(f"{detail} (all proxies)", exc.retry_after)
        return SearchUnavailable(detail, self.guard.proxy_quarantine)

    async def cheapest(self, *args, **kwargs) -> Quote | None:
        quotes = await self.search(*args, **kwargs)
        return quotes[0] if quotes else None

    async def maintenance(self):
        await self.cache.purge()
        await self.guard.maintenance()

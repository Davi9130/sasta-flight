from __future__ import annotations

import json
import time
from collections.abc import Callable

from bot.config import QUOTE_CACHE_TTL_MINS
from bot.db import Database
from bot.fares.models import Quote


def cache_key(
    from_airport: str,
    to_airport: str,
    date: str,
    return_date: str | None,
    max_stops: str,
    currency: str,
) -> str:
    return "|".join((from_airport, to_airport, date, return_date or "", max_stops, currency))


class QuoteCache:
    def __init__(
        self,
        db: Database,
        ttl_mins: int = QUOTE_CACHE_TTL_MINS,
        clock: Callable[[], float] = time.time,
    ):
        self.db = db
        self.ttl = ttl_mins * 60
        self.clock = clock

    async def get(self, key: str) -> list[Quote] | None:
        payload = await self.db.cache_get(key, self.clock() - self.ttl)
        if payload is None:
            return None
        return [Quote.from_dict(item) for item in json.loads(payload)]

    async def put(self, key: str, quotes: list[Quote]):
        await self.db.cache_put(key, self.clock(), json.dumps([q.to_dict() for q in quotes]))

    async def purge(self):
        await self.db.cache_purge(self.clock() - self.ttl)

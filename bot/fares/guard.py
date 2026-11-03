"""Every Google request asks the guard first. State lives in SQLite so restarts don't reset it."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import time
from collections.abc import Awaitable, Callable

from bot.config import (
    BLOCK_COOLDOWN_MAX_SECS,
    BLOCK_COOLDOWN_SECS,
    PROXY_QUARANTINE_SECS,
    PROXY_URLS,
    REQ_BUDGET_PER_DAY,
    REQ_BUDGET_PER_HOUR,
    REQ_JITTER_MAX_SECS,
    REQ_JITTER_MIN_SECS,
)
from bot.db import Database
from bot.fares.client import BLOCKED, NO_FLIGHTS, OK
from bot.fares.models import SearchUnavailable

logger = logging.getLogger(__name__)

HOUR = 3600.0
DAY = 86400.0


def proxy_id(proxy: str | None) -> str:
    """Stable, credential-free label for logs and state keys."""
    if proxy is None:
        return "direct"
    return "p-" + hashlib.sha1(proxy.encode()).hexdigest()[:10]


class Guard:
    def __init__(
        self,
        db: Database,
        *,
        proxies: list[str] | None = None,
        per_hour: int = REQ_BUDGET_PER_HOUR,
        per_day: int = REQ_BUDGET_PER_DAY,
        jitter: tuple[float, float] = (REQ_JITTER_MIN_SECS, REQ_JITTER_MAX_SECS),
        block_cooldown: float = BLOCK_COOLDOWN_SECS,
        block_cooldown_max: float = BLOCK_COOLDOWN_MAX_SECS,
        proxy_quarantine: float = PROXY_QUARANTINE_SECS,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ):
        self.db = db
        self.proxies = list(PROXY_URLS if proxies is None else proxies)
        self.per_hour = per_hour
        self.per_day = per_day
        self.jitter = jitter
        self.block_cooldown = block_cooldown
        self.block_cooldown_max = block_cooldown_max
        self.proxy_quarantine = proxy_quarantine
        self.clock = clock
        self.sleep = sleep
        self.rng = rng or random.Random()
        self.lock = asyncio.Lock()
        self._rr = 0

    # --- circuit ---
    async def circuit(self) -> tuple[str | None, float]:
        """(reason, seconds remaining); (None, 0) when closed."""
        until = float(await self.db.get_guard("circuit_until") or 0)
        remaining = until - self.clock()
        if remaining <= 0:
            return None, 0.0
        return await self.db.get_guard("circuit_reason") or "blocked", remaining

    async def _open_circuit(self, reason: str) -> float:
        strikes = int(await self.db.get_guard("consecutive_blocks") or 0) + 1
        cooldown = min(self.block_cooldown * (2 ** (strikes - 1)), self.block_cooldown_max)
        await self.db.set_guard("consecutive_blocks", str(strikes))
        await self.db.set_guard("circuit_until", str(self.clock() + cooldown))
        await self.db.set_guard("circuit_reason", reason)
        logger.warning("Circuit open: %s (strike %d) for %.0fs", reason, strikes, cooldown)
        return cooldown

    # --- budgets ---
    async def _budget_wait(self) -> tuple[str | None, float]:
        now = self.clock()
        times = await self.db.request_times_since(now - DAY)
        if len(times) >= self.per_day:
            return "budget_day", times[-self.per_day] + DAY - now
        hour = [t for t in times if t >= now - HOUR]
        if len(hour) >= self.per_hour:
            return "budget_hour", hour[-self.per_hour] + HOUR - now
        return None, 0.0

    async def check(self):
        reason, remaining = await self.circuit()
        if reason:
            raise SearchUnavailable(reason, remaining)
        reason, remaining = await self._budget_wait()
        if reason:
            raise SearchUnavailable(reason, remaining)

    # --- proxies ---
    async def _proxy_quarantined_for(self, proxy: str) -> float:
        until = float(await self.db.get_guard(f"proxy_until:{proxy_id(proxy)}") or 0)
        return max(0.0, until - self.clock())

    async def pick_proxy(self) -> str | None:
        if not self.proxies:
            return None
        waits = []
        for offset in range(len(self.proxies)):
            idx = (self._rr + offset) % len(self.proxies)
            proxy = self.proxies[idx]
            wait = await self._proxy_quarantined_for(proxy)
            if wait <= 0:
                self._rr = idx + 1
                return proxy
            waits.append(wait)
        raise SearchUnavailable("proxies_blocked", min(waits))

    # --- pacing ---
    async def pace(self):
        """Human-ish spacing between requests, measured from the last request on record."""
        last = await self.db.last_request_at()
        if last is None:
            return
        gap = self.rng.uniform(*self.jitter)
        wait = last + gap - self.clock()
        if wait > 0:
            await self.sleep(wait)

    async def record(self, outcome: str, proxy: str | None, detail: str | None = None):
        await self.db.log_request(self.clock(), outcome, proxy_id(proxy), detail)
        if outcome in (OK, NO_FLIGHTS):
            await self.db.set_guard("consecutive_blocks", "0")
            return
        if outcome != BLOCKED:
            return
        if proxy is not None:
            until = self.clock() + self.proxy_quarantine
            await self.db.set_guard(f"proxy_until:{proxy_id(proxy)}", str(until))
            logger.warning("Proxy %s quarantined (%s)", proxy_id(proxy), detail)
            return
        await self._open_circuit(detail or "blocked")

    async def snapshot(self) -> dict:
        now = self.clock()
        times = await self.db.request_times_since(now - DAY)
        reason, remaining = await self.circuit()
        proxies = []
        for proxy in self.proxies:
            proxies.append((proxy_id(proxy), await self._proxy_quarantined_for(proxy)))
        return {
            "requests_hour": sum(1 for t in times if t >= now - HOUR),
            "requests_day": len(times),
            "per_hour": self.per_hour,
            "per_day": self.per_day,
            "circuit_reason": reason,
            "circuit_remaining": remaining,
            "proxies": proxies,
        }

    async def maintenance(self):
        await self.db.purge_request_log(self.clock() - 2 * DAY)

import os

os.environ.setdefault("TELEGRAM_CHAT_ID", "42")
os.environ["PROXY_URLS"] = ""

from datetime import datetime  # noqa: E402

import pytest  # noqa: E402

from bot.db import Database  # noqa: E402
from bot.fares.models import BudgetExhausted, Quote, SearchUnavailable  # noqa: E402


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.init()
    yield database
    await database.close()


class FakeClock:
    def __init__(self, start: float = 1_000_000.0):
        self.now = start
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, secs: float):
        self.slept.append(secs)
        self.now += secs


@pytest.fixture
def clock():
    return FakeClock()


class FakeGuard:
    proxies: list = []

    async def circuit(self):
        return None, 0.0


class FakeFares:
    """Prices come from `price_fn(from, to, date, return_date)`; None means no flights."""

    name = "fake"

    def __init__(self, price_fn, *, block_after: int | None = None, times=None):
        self.price_fn = price_fn
        self.block_after = block_after
        self.calls: list[tuple] = []
        self.guard = FakeGuard()
        self.times = times or (lambda f, t, d: (datetime.fromisoformat(d + "T10:00"), datetime.fromisoformat(d + "T12:00")))

    async def search(self, f, t, d, r=None, *, max_stops="any", currency, budget=None):
        if self.block_after is not None and len(self.calls) >= self.block_after:
            raise SearchUnavailable("http_429", 1800)
        if budget is not None:
            if budget.remaining <= 0:
                raise BudgetExhausted()
            budget.spend()
        self.calls.append((f, t, d, r))
        price = self.price_fn(f, t, d, r)
        if price is None:
            return []
        dep, arr = self.times(f, t, d)
        return [
            Quote(f, t, d, r, float(price), currency, ["TestAir"], dep, arr, 600, 1, f"https://x/{f}{t}{d}")
        ]

    async def cheapest(self, *args, **kwargs):
        quotes = await self.search(*args, **kwargs)
        return quotes[0] if quotes else None

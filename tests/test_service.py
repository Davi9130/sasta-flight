import pytest

from bot.fares.cache import QuoteCache
from bot.fares.client import BLOCKED, ERROR, NO_FLIGHTS, OK, FetchResult
from bot.fares.guard import Guard
from bot.fares.models import BudgetExhausted, Quote, SearchFailed, SearchUnavailable
from bot.fares.service import FareService, ScanBudget


class ScriptedClient:
    name = "scripted"

    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.resets = []

    def fetch(self, f, t, d, r, *, max_stops, currency, proxy):
        self.calls.append((f, t, d, r, proxy))
        return self.results.pop(0)

    def reset(self, proxy):
        self.resets.append(proxy)


def _ok(price=500.0):
    return FetchResult(OK, quotes=[Quote("VIX", "GRU", "2026-12-01", None, price, "BRL")])


def _service(db, clock, results, proxies=None, retries=1):
    client = ScriptedClient(results)
    guard = Guard(db, proxies=proxies or [], jitter=(1, 2), clock=clock, sleep=clock.sleep)
    return FareService(client, guard, QuoteCache(db, clock=clock), error_retries=retries), client


async def _search(service, **kwargs):
    return await service.search("VIX", "GRU", "2026-12-01", currency="BRL", **kwargs)


async def test_cache_hit_skips_google(db, clock):
    service, client = _service(db, clock, [_ok()])
    first = await _search(service)
    second = await _search(service)
    assert first[0].price == second[0].price == 500
    assert len(client.calls) == 1


async def test_no_flights_is_cached_too(db, clock):
    service, client = _service(db, clock, [FetchResult(NO_FLIGHTS)])
    assert await _search(service) == []
    assert await _search(service) == []
    assert len(client.calls) == 1


async def test_block_opens_circuit_and_stops_further_calls(db, clock):
    service, client = _service(db, clock, [FetchResult(BLOCKED, detail="captcha")])
    with pytest.raises(SearchUnavailable) as exc:
        await _search(service)
    assert exc.value.reason == "captcha"
    with pytest.raises(SearchUnavailable):
        await service.search("VIX", "GRU", "2026-12-02", currency="BRL")
    assert len(client.calls) == 1


async def test_error_retried_then_failed(db, clock):
    service, client = _service(db, clock, [FetchResult(ERROR, detail="timeout"), _ok()])
    assert (await _search(service))[0].price == 500
    service, client = _service(db, clock, [FetchResult(ERROR, detail="x")] * 2)
    with pytest.raises(SearchFailed):
        await service.search("VIX", "GRU", "2026-12-09", currency="BRL")
    assert len(client.calls) == 2


async def test_blocked_proxy_falls_over_to_next(db, clock):
    service, client = _service(
        db, clock, [FetchResult(BLOCKED, detail="http_429"), _ok(420)], proxies=["http://a:1", "http://b:2"]
    )
    quotes = await _search(service)
    assert quotes[0].price == 420
    assert [c[4] for c in client.calls] == ["http://a:1", "http://b:2"]
    assert client.resets == ["http://a:1"]


async def test_scan_budget_counts_only_network(db, clock):
    service, client = _service(db, clock, [_ok()])
    budget = ScanBudget(1)
    await _search(service, budget=budget)
    await _search(service, budget=budget)  # cached: free
    assert budget.used == 1
    with pytest.raises(BudgetExhausted):
        await service.search("VIX", "GRU", "2026-12-05", currency="BRL", budget=budget)

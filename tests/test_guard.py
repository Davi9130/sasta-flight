import pytest

from bot.fares.client import BLOCKED, ERROR, OK
from bot.fares.guard import Guard
from bot.fares.models import SearchUnavailable


def _guard(db, clock, **kwargs):
    kwargs.setdefault("proxies", [])
    kwargs.setdefault("jitter", (4.0, 9.0))
    kwargs.setdefault("block_cooldown", 1800)
    kwargs.setdefault("block_cooldown_max", 6 * 3600)
    return Guard(db, clock=clock, sleep=clock.sleep, **kwargs)


async def test_circuit_survives_restart(db, clock):
    await _guard(db, clock).record(BLOCKED, None, "http_429")
    fresh = _guard(db, clock)  # simulates a process restart
    with pytest.raises(SearchUnavailable) as exc:
        await fresh.check()
    assert exc.value.reason == "http_429"
    assert exc.value.retry_after == pytest.approx(1800)


async def test_cooldown_escalates_and_ok_resets(db, clock):
    guard = _guard(db, clock)
    await guard.record(BLOCKED, None, "captcha")
    clock.now += 1801
    await guard.record(BLOCKED, None, "captcha")
    assert (await guard.circuit())[1] == pytest.approx(3600)

    clock.now += 3601
    await guard.check()
    await guard.record(OK, None)
    await guard.record(BLOCKED, None, "captcha")
    assert (await guard.circuit())[1] == pytest.approx(1800)


async def test_errors_do_not_open_circuit(db, clock):
    guard = _guard(db, clock)
    await guard.record(ERROR, None, "timeout")
    await guard.check()


async def test_hourly_and_daily_budget(db, clock):
    guard = _guard(db, clock, per_hour=3, per_day=5)
    for _ in range(3):
        await guard.record(OK, None)
        clock.now += 10
    with pytest.raises(SearchUnavailable) as exc:
        await guard.check()
    assert exc.value.reason == "budget_hour"
    assert exc.value.retry_after == pytest.approx(3600 - 30)

    clock.now += 3600
    await guard.check()
    for _ in range(2):
        await guard.record(OK, None)
    with pytest.raises(SearchUnavailable) as exc:
        await guard.check()
    assert exc.value.reason == "budget_day"


async def test_proxy_rotation_and_quarantine(db, clock):
    guard = _guard(db, clock, proxies=["http://a:1", "http://b:2"], proxy_quarantine=600)
    first = await guard.pick_proxy()
    second = await guard.pick_proxy()
    assert {first, second} == {"http://a:1", "http://b:2"}

    await guard.record(BLOCKED, "http://a:1", "http_429")
    assert await guard.pick_proxy() == "http://b:2"
    assert await guard.pick_proxy() == "http://b:2"
    await guard.check()  # a proxy block does not stop the whole bot

    await guard.record(BLOCKED, "http://b:2", "captcha")
    with pytest.raises(SearchUnavailable) as exc:
        await guard.pick_proxy()
    assert exc.value.reason == "proxies_blocked"

    clock.now += 601
    assert await guard.pick_proxy() in {"http://a:1", "http://b:2"}


async def test_pace_waits_between_requests(db, clock):
    guard = _guard(db, clock)
    await guard.pace()
    assert clock.slept == []
    await guard.record(OK, None)
    await guard.pace()
    assert 4.0 <= clock.slept[-1] <= 9.0


async def test_snapshot(db, clock):
    guard = _guard(db, clock, proxies=["http://user:secret@a:1"])
    await guard.record(OK, "http://user:secret@a:1")
    snap = await guard.snapshot()
    assert snap["requests_hour"] == 1
    assert "secret" not in str(snap)

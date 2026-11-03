from bot import runner
from bot.runner import ALERT, DEFERRED, NO_MATCH, OK, SKIPPED, scan_and_notify
from bot.services import Services
from tests.conftest import FakeFares


class FakeBot:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)


def _svc(db, fares):
    return Services(db=db, fares=fares, fx=None)


async def test_silent_scan_saves_history(db):
    rid = await db.add_route("VIX", "GRU")
    bot = FakeBot()
    outcome = await scan_and_notify(_svc(db, FakeFares(lambda *a: 900)), bot, rid)
    assert outcome == OK and bot.sent == []
    assert len(await db.get_price_history(rid)) == 1
    assert await db.get_previous_cheapest(rid) == 900


async def test_target_hit_sends_markdown_alert_once(db):
    rid = await db.add_route("VIX", "GRU")
    await db.set_route_alert(rid, target_price=1000)
    bot = FakeBot()
    svc = _svc(db, FakeFares(lambda *a: 900))
    assert await scan_and_notify(svc, bot, rid) == ALERT
    assert bot.sent[0]["parse_mode"] == "Markdown"
    assert "Target hit" in bot.sent[0]["text"]
    assert await scan_and_notify(svc, bot, rid) == OK  # cooldown
    assert len(bot.sent) == 1


async def test_deferred_notifies_once_per_episode_and_schedules_retry(db):
    r1 = await db.add_route("VIX", "GRU")
    r2 = await db.add_route("VIX", "GIG")
    bot = FakeBot()
    retries = []
    svc = _svc(db, FakeFares(lambda *a: 900, block_after=0))
    for rid in (r1, r2):
        outcome = await scan_and_notify(
            svc, bot, rid, schedule_retry=lambda route_id, delay, is_retry: retries.append((route_id, delay))
        )
        assert outcome == DEFERRED
    assert len(bot.sent) == 1 and "postponed" in bot.sent[0]["text"]
    assert [r for r, _ in retries] == [r1, r2]
    assert all(delay >= 1800 for _, delay in retries)


async def test_no_matches_and_skipped(db):
    rid = await db.add_route("VIX", "GRU")
    bot = FakeBot()
    assert await scan_and_notify(_svc(db, FakeFares(lambda *a: None)), bot, rid) == NO_MATCH
    assert "No flights found" in bot.sent[0]["text"]
    await db.remove_route(rid)
    assert await scan_and_notify(_svc(db, FakeFares(lambda *a: 1)), bot, rid) == SKIPPED


async def test_summary_mode_sends_full_message(db, monkeypatch):
    monkeypatch.setattr(runner, "ALWAYS_SEND_SCAN_SUMMARY", True)
    rid = await db.add_route("VIX", "GRU")
    bot = FakeBot()
    assert await scan_and_notify(_svc(db, FakeFares(lambda *a: 900)), bot, rid) == ALERT
    assert "Top" in bot.sent[0]["text"]

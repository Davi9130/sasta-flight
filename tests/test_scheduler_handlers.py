import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

from bot import handlers
from bot.scheduler import SCAN_JOB_PREFIX, ScanQueue, first_run_delay, schedule_scan_jobs
from bot.services import SERVICES_KEY, Services
from tests.conftest import FakeFares


async def test_queue_is_serial_and_dedupes():
    running = []
    order = []

    async def runner(route_id, manual, is_retry):
        running.append(route_id)
        assert len(running) == 1, "two scans at once"
        await asyncio.sleep(0.01)
        order.append(route_id)
        running.remove(route_id)
        return "ok"

    queue = ScanQueue(runner)
    queue.start()
    a = queue.enqueue(1)
    b = queue.enqueue(2)
    assert queue.enqueue(1) is a
    assert await asyncio.gather(a, b) == ["ok", "ok"]
    assert order == [1, 2]
    await queue.stop()


def test_first_run_delay():
    now = datetime(2026, 11, 3, 9, 0)
    assert first_run_delay(now, 10, 0, 3600) == timedelta(hours=1)
    assert first_run_delay(now, 8, 0, 3600) == timedelta(hours=1)
    assert first_run_delay(now, 8, 30, 86400) == timedelta(hours=23, minutes=30)


class FakeJobQueue:
    def __init__(self):
        self._jobs = []

    def jobs(self):
        return [j for j in self._jobs if not j.removed]

    def get_jobs_by_name(self, name):
        return [j for j in self.jobs() if j.name == name]

    def _add(self, name, **kw):
        job = SimpleNamespace(name=name, removed=False, **kw)
        job.schedule_removal = lambda: setattr(job, "removed", True)
        self._jobs.append(job)
        return job

    def run_repeating(self, callback, interval, first, data, name):
        return self._add(name, interval=interval, first=first, data=data)

    def run_once(self, callback, when, data, name):
        return self._add(name, when=when, data=data)


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


def _context(db, args):
    application = SimpleNamespace(
        bot_data={SERVICES_KEY: Services(db=db, fares=FakeFares(lambda *a: 1))},
        job_queue=FakeJobQueue(),
        create_task=lambda coro: asyncio.ensure_future(coro),
    )
    return SimpleNamespace(args=args, application=application, bot_data={}, job_queue=application.job_queue)


def _update(chat_id=42):
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id), message=FakeMessage())


async def test_add_route_schedules_job(db):
    update, context = _update(), _context(db, ["vix,gig", "MXP", "7-10"])
    await handlers.add_command(update, context)
    text, kwargs = update.message.replies[0]
    assert "VIX,GIG ⇄ MXP (7-10d)" in text and kwargs["reply_markup"]
    routes = await db.get_active_routes()
    assert len(routes) == 1
    assert context.job_queue.get_jobs_by_name(f"{SCAN_JOB_PREFIX}{routes[0]['id']}")


async def test_add_rejects_bad_input_and_unauthorized(db):
    update = _update()
    await handlers.add_command(update, _context(db, ["VI", "MXP"]))
    assert "3 letters" in update.message.replies[0][0]
    update = _update()
    await handlers.add_command(update, _context(db, ["VIX", "MXP", "0"]))
    assert "between" in update.message.replies[0][0]
    stranger = _update(chat_id=7)
    await handlers.add_command(stranger, _context(db, ["VIX", "MXP"]))
    assert stranger.message.replies == []
    assert await db.get_active_routes() == []


async def test_alert_and_remove_commands(db):
    rid = await db.add_route("VIX", "GRU")
    update = _update()
    await handlers.alert_command(update, _context(db, [str(rid), "target", "2800"]))
    assert (await db.get_route(rid))["target_price"] == 2800
    await handlers.alert_command(update, _context(db, [str(rid), "low", "on"]))
    assert (await db.get_route(rid))["alert_on_new_low"] == 1

    context = _context(db, [str(rid)])
    await schedule_scan_jobs(context.application)
    await handlers.remove_command(update, context)
    assert "removed" in update.message.replies[-1][0]
    assert context.job_queue.get_jobs_by_name(f"{SCAN_JOB_PREFIX}{rid}") == []


async def test_check_reports_when_circuit_open(db):
    await db.add_route("VIX", "GRU")

    class BlockedGuard:
        proxies = []

        async def circuit(self):
            return "captcha", 900.0

    context = _context(db, [])
    context.application.bot_data[SERVICES_KEY].fares.guard = BlockedGuard()
    update = _update()
    await handlers.check_command(update, context)
    assert "captcha" in update.message.replies[0][0]


async def test_check_enqueues_and_reports(db):
    await db.add_route("VIX", "GRU")
    context = _context(db, [])
    svc = context.application.bot_data[SERVICES_KEY]
    svc.queue = ScanQueue(lambda rid, manual, retry: asyncio.sleep(0, result="ok"))
    svc.queue.start()
    update = _update()
    await handlers.check_command(update, context)
    await asyncio.sleep(0.05)
    assert "Scan done" in update.message.replies[-1][0]
    await svc.queue.stop()

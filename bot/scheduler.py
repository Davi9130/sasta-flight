"""Per-route repeating jobs feed one serial queue: only one scan talks to Google at a time."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from bot.config import SCHEDULE_JITTER_MINS, TIMEZONE
from bot.services import get_services

logger = logging.getLogger(__name__)

SCAN_JOB_PREFIX = "scan_route_"
JITTER_JOB_PREFIX = "jitter_route_"
RETRY_JOB_PREFIX = "retry_"
ROUTE_STAGGER_SECS = 120

Runner = Callable[[int, bool, bool], Awaitable[str]]


class ScanQueue:
    def __init__(self, runner: Runner):
        self.runner = runner
        self._queue: asyncio.Queue[tuple[int, bool, bool]] = asyncio.Queue()
        self._pending: dict[int, asyncio.Future] = {}
        self._worker: asyncio.Task | None = None
        self.current: int | None = None

    def start(self):
        if self._worker is None or self._worker.done():
            self._worker = asyncio.get_running_loop().create_task(self._run())

    async def stop(self):
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None

    def enqueue(self, route_id: int, *, manual: bool = False, is_retry: bool = False) -> asyncio.Future:
        """Queue a scan; a route already waiting is not queued twice."""
        existing = self._pending.get(route_id)
        if existing is not None and not existing.done():
            return existing
        fut = asyncio.get_running_loop().create_future()
        self._pending[route_id] = fut
        self._queue.put_nowait((route_id, manual, is_retry))
        return fut

    @property
    def size(self) -> int:
        return self._queue.qsize()

    async def _run(self):
        while True:
            route_id, manual, is_retry = await self._queue.get()
            fut = self._pending.get(route_id)
            self.current = route_id
            try:
                outcome = await self.runner(route_id, manual, is_retry)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Scan worker crashed on route %s", route_id)
                outcome = "error"
            finally:
                self.current = None
                self._pending.pop(route_id, None)
                self._queue.task_done()
            if fut is not None and not fut.done():
                fut.set_result(outcome)


def first_run_delay(now: datetime, notify_hour: int, notify_minute: int, interval_secs: int) -> timedelta:
    """Delay until the next slot aligned to the daily start time."""
    anchor = now.replace(hour=notify_hour, minute=notify_minute, second=0, microsecond=0)
    if now < anchor:
        return anchor - now
    slots = int((now - anchor).total_seconds() // interval_secs) + 1
    return anchor + timedelta(seconds=slots * interval_secs) - now


async def schedule_scan_jobs(application):
    svc = get_services(application)
    jq = application.job_queue
    for job in jq.jobs():
        if job.name and job.name.startswith(SCAN_JOB_PREFIX):
            job.schedule_removal()

    notify_time = await svc.db.get_config("notify_time") or "08:00"
    hour, minute = map(int, notify_time.split(":"))
    now = datetime.now(ZoneInfo(TIMEZONE))
    routes = await svc.db.get_active_routes()
    for i, route in enumerate(routes):
        interval_secs = await svc.db.get_route_scan_interval(route) * 60
        first = first_run_delay(now, hour, minute, interval_secs) + timedelta(seconds=i * ROUTE_STAGGER_SECS)
        jq.run_repeating(
            _scheduled_tick,
            interval=interval_secs,
            first=first,
            data=route["id"],
            name=f"{SCAN_JOB_PREFIX}{route['id']}",
        )
    logger.info("Scheduled %d route job(s) (start %s %s)", len(routes), notify_time, TIMEZONE)


def cancel_route_jobs(application, route_id: int):
    for prefix in (SCAN_JOB_PREFIX, JITTER_JOB_PREFIX, RETRY_JOB_PREFIX):
        for job in application.job_queue.get_jobs_by_name(f"{prefix}{route_id}"):
            job.schedule_removal()


def schedule_retry(application, route_id: int, delay_secs: float, is_retry: bool):
    name = f"{RETRY_JOB_PREFIX}{route_id}"
    if application.job_queue.get_jobs_by_name(name):
        return
    application.job_queue.run_once(
        _enqueue_job, when=delay_secs, data={"route_id": route_id, "is_retry": is_retry}, name=name
    )


async def _scheduled_tick(context):
    svc = get_services(context.application)
    if await svc.db.get_config("is_paused") == "1":
        return
    route_id = context.job.data
    delay = random.uniform(0, SCHEDULE_JITTER_MINS * 60)
    context.job_queue.run_once(
        _enqueue_job, when=delay, data={"route_id": route_id}, name=f"{JITTER_JOB_PREFIX}{route_id}"
    )


async def _enqueue_job(context):
    svc = get_services(context.application)
    if await svc.db.get_config("is_paused") == "1":
        return
    data = context.job.data or {}
    svc.queue.enqueue(data["route_id"], is_retry=bool(data.get("is_retry")))

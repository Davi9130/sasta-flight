"""Scan one saved route end to end: search, persist, evaluate alerts, notify."""

from __future__ import annotations

import logging
import time

from bot.alerts import evaluate_alerts
from bot.config import ALWAYS_SEND_SCAN_SUMMARY, CHAT_ID, CURRENCY, MIN_STAY_DAYS
from bot.db import today_local
from bot.formatter import (
    format_alert_only_message,
    format_daily_message,
    format_deferred_message,
    format_error_message,
    format_no_matches_message,
    format_via_message,
    route_label,
)
from bot.search.hubs import scan_via_route
from bot.search.scan import NO_MATCHES, ScanDeferred, ScanResult, parse_airport_list, scan_route
from bot.services import Services

logger = logging.getLogger(__name__)

STOPS_LABELS = {"any": "Any", "direct": "Direct", "1stop": "Up to 1 Stop", "2stops": "Up to 2 Stops"}

ALERT = "alert"
OK = "ok"
DEFERRED = "deferred"
NO_MATCH = "no_matches"
ERROR = "error"
SKIPPED = "skipped"

DEFER_NOTICE_KEY = "defer_notice_until"


async def run_scan(svc: Services, route: dict, max_stops: str) -> ScanResult | object:
    origins = parse_airport_list(route["from_airport"])
    destinations = parse_airport_list(route["to_airport"])
    if route.get("via_hub"):
        return await scan_via_route(
            svc.fares,
            origins=origins,
            destinations=destinations,
            stay_min=int(route.get("stay_min") or MIN_STAY_DAYS),
            stay_max=route.get("stay_max"),
            hub_nights_min=int(route.get("hub_nights_min") or 0),
            hub_nights_max=route.get("hub_nights_max"),
            max_stops=max_stops,
            currency=CURRENCY,
        )
    return await scan_route(
        svc.fares,
        origins=origins,
        destinations=destinations,
        stay_min=route.get("stay_min"),
        stay_max=route.get("stay_max"),
        max_stops=max_stops,
        currency=CURRENCY,
        pair_priority=await svc.db.get_pair_best_prices(route["id"]),
    )


async def _send(bot, text: str, markdown: bool = False):
    await bot.send_message(
        chat_id=CHAT_ID,
        text=text,
        parse_mode="Markdown" if markdown else None,
        disable_web_page_preview=True,
    )


async def _notify_deferred(svc: Services, bot, route: dict, exc: ScanDeferred, *, manual: bool):
    """One notice per block episode, not one per route."""
    now = time.time()
    until = float(await svc.db.get_guard(DEFER_NOTICE_KEY) or 0)
    if not manual and now < until:
        return
    await svc.db.set_guard(DEFER_NOTICE_KEY, str(now + exc.retry_after))
    await _send(bot, format_deferred_message(route_label(route), exc.reason, exc.retry_after))


async def scan_and_notify(
    svc: Services,
    bot,
    route_id: int,
    *,
    manual: bool = False,
    is_retry: bool = False,
    schedule_retry=None,
) -> str:
    """Returns one of ALERT, OK, DEFERRED, NO_MATCH, ERROR, SKIPPED.

    `schedule_retry(route_id, delay_secs, is_retry)` is provided by the scheduler.
    """
    db = svc.db
    route = await db.get_route(route_id)
    if not route or not route.get("is_active"):
        return SKIPPED

    label = route_label(route)
    max_stops = await db.get_route_stops_preference(route)
    filters = {
        "max_stops": max_stops,
        "stay_min": route.get("stay_min"),
        "stay_max": route.get("stay_max"),
        "via_hub": bool(route.get("via_hub")),
    }
    prev_cheapest = await db.get_previous_cheapest(route_id)
    stats_before = await db.get_route_price_stats(route_id, days=30)
    started = time.monotonic()

    try:
        result = await run_scan(svc, route, max_stops)
    except ScanDeferred as exc:
        await db.create_scan_run(
            route_id,
            DEFERRED,
            provider=svc.fares.name,
            currency=CURRENCY,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=f"{exc.reason}; retry_after={exc.retry_after:.0f}s",
            requests_used=exc.requests_used,
            filters=filters,
        )
        await _notify_deferred(svc, bot, route, exc, manual=manual)
        if schedule_retry:
            schedule_retry(route_id, max(120.0, exc.retry_after + 60), False)
        return DEFERRED
    except Exception as exc:
        logger.exception("Scan failed for route %s", route_id)
        await db.create_scan_run(
            route_id,
            ERROR,
            provider=svc.fares.name,
            currency=CURRENCY,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=f"{type(exc).__name__}: {exc}",
            filters=filters,
        )
        if is_retry:
            await _send(bot, format_error_message(label))
        elif await db.get_route_scan_interval(route) > 240 and schedule_retry:
            await _send(bot, format_error_message(label, retry_hours=4))
            schedule_retry(route_id, 4 * 3600, True)
        else:
            await _send(bot, format_error_message(label))
        return ERROR

    duration_ms = int((time.monotonic() - started) * 1000)
    if result is NO_MATCHES:
        await db.create_scan_run(
            route_id, NO_MATCH, provider=svc.fares.name, currency=CURRENCY,
            duration_ms=duration_ms, filters=filters,
        )
        await _send(bot, format_no_matches_message(label, STOPS_LABELS.get(max_stops, max_stops)))
        return NO_MATCH

    scan_run_id = await db.create_scan_run(
        route_id,
        OK,
        provider=result.provider,
        currency=result.currency,
        duration_ms=duration_ms,
        cheapest_price=result.cheapest_price,
        cheapest_travel_date=result.cheapest_travel_date,
        cheapest_return_date=result.cheapest_return_date,
        fare_type=result.fare_type,
        requests_used=result.requests_used,
        partial=result.partial,
        filters=filters,
    )
    await db.save_fare_snapshots(route_id, scan_run_id, result.top_days, result.currency)
    await db.save_price_history(
        route_id,
        scan_date=today_local(),
        cheapest_travel_date=result.cheapest_travel_date,
        cheapest_return_date=result.cheapest_return_date,
        cheapest_price=result.cheapest_price,
        cheapest_airline=result.cheapest_airline,
        avg_price=result.avg_price,
        price_data=result.top_days,
        currency=result.currency,
        provider=result.provider,
        fare_type=result.fare_type,
    )

    fx_amounts = None
    if svc.fx:
        try:
            fx_amounts = await svc.fx.convert(result.cheapest_price, base=result.currency)
        except Exception:
            logger.exception("FX conversion failed")

    hits = await evaluate_alerts(
        db,
        route,
        price=result.cheapest_price,
        travel_date=result.cheapest_travel_date,
        prev_price=prev_cheapest,
        stats=stats_before,
        currency=result.currency,
    )
    alert_lines = [
        hit.message
        for hit in hits
        if await db.record_alert_event(route_id, hit.rule, hit.price, hit.fingerprint, currency=result.currency)
    ]

    stops_label = STOPS_LABELS.get(max_stops) if max_stops != "any" else None
    if ALWAYS_SEND_SCAN_SUMMARY or (alert_lines and result.fare_type == "via_hub"):
        if result.fare_type == "via_hub":
            msg = format_via_message(
                result, max_stops=max_stops, fx_amounts=fx_amounts,
                alert_lines=alert_lines or None, prev_cheapest=prev_cheapest,
            )
        else:
            msg = format_daily_message(
                result, prev_cheapest=prev_cheapest, stops_label=stops_label, max_stops=max_stops,
                fx_amounts=fx_amounts, alert_lines=alert_lines or None,
                stats=stats_before if stats_before.get("count") else None,
            )
        await _send(bot, msg, markdown=True)
        return ALERT
    if alert_lines:
        await _send(bot, format_alert_only_message(result, alert_lines, fx_amounts=fx_amounts), markdown=True)
        return ALERT
    return OK

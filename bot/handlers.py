import json
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.alerts import evaluate_alerts
from bot.config import (
    ALWAYS_SEND_SCAN_SUMMARY,
    CHAT_ID,
    COUNTRY,
    CURRENCY,
    DEFAULT_ALERT_COOLDOWN_MINUTES,
    DEFAULT_ALERT_DROP_PCT,
    INTERVAL_OPTIONS,
    MAX_STAY_DAYS,
    MIN_STAY_DAYS,
    TIMEZONE,
)
from bot.db import Database
from bot.formatter import (
    format_alert_only_message,
    format_daily_message,
    format_error_message,
    format_history_message,
    format_retry_failed_message,
)
from bot.fx import FxService
from bot.scanner import NO_MATCHES, parse_airport_list, parse_stay_range, scan_route

logger = logging.getLogger(__name__)

db: Database = None
fx_service: FxService | None = None

STOPS_LABELS = {
    "any": "Any",
    "direct": "Direct",
    "1stop": "Up to 1 Stop",
    "2stops": "Up to 2 Stops",
}

INTERVAL_LABELS = {str(v): k for k, v in INTERVAL_OPTIONS.items()}


def _frequency_keyboard(callback_prefix: str, current_minutes: str | None = None) -> InlineKeyboardMarkup:
    buttons = []
    for label, minutes in INTERVAL_OPTIONS.items():
        display = f">> {label} <<" if str(minutes) == current_minutes else label
        buttons.append(InlineKeyboardButton(display, callback_data=f"{callback_prefix}:{minutes}"))
    return InlineKeyboardMarkup([buttons[:3], buttons[3:]])


def _stops_keyboard(callback_prefix: str, current: str | None = None) -> InlineKeyboardMarkup:
    buttons = []
    for value, label in STOPS_LABELS.items():
        display = f">> {label} <<" if value == current else label
        buttons.append(InlineKeyboardButton(display, callback_data=f"{callback_prefix}:{value}"))
    return InlineKeyboardMarkup([buttons])


def _is_authorized(update: Update) -> bool:
    return update.effective_chat.id == CHAT_ID


def _help_text() -> str:
    return (
        "✈️ SastaFlight - Flight Price Scanner\n\n"
        "Commands:\n"
        "/add <from> <to> [days|min-max] - Add a route\n"
        "  Examples:\n"
        "  /add VIX MXP — one-way\n"
        "  /add VIX,GIG MXP,BGY 10 — multi-airport, 10-day stay\n"
        "  /add VIX MXP 7-10 — flexible stay range\n"
        "/remove <id> - Remove a route\n"
        "/routes - List active routes\n"
        "/stops - Set default stops preference\n"
        "/frequency - Set scan frequency\n"
        "/alert <id> target <price> - Alert when price ≤ target\n"
        "/alert <id> drop <pct> - Alert on % drop vs last scan\n"
        "/alert <id> clear - Clear target price\n"
        "/check - Scan all routes now\n"
        f"/time <HH:MM> - Set scan start time (24h, {TIMEZONE})\n"
        "/history - Price trend + stats\n"
        "/pause - Pause scheduled scans\n"
        "/resume - Resume scheduled scans\n"
        "/help - Show this message\n\n"
        f"Prices shown in {CURRENCY} with ≈ EUR / USD when available."
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    await update.message.reply_text(_help_text())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    await start_command(update, context)


def _normalize_airport_arg(raw: str) -> str | None:
    codes = parse_airport_list(raw)
    if not codes:
        return None
    for code in codes:
        if len(code) != 3 or not code.isalpha():
            return None
    return ",".join(codes)


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    if not context.args or len(context.args) not in (2, 3):
        await update.message.reply_text(
            "Usage: /add <from> <to> [days|min-max]\n"
            "Examples:\n"
            "/add ATQ BOM — one-way\n"
            "/add VIX MXP 10 — round-trip, 10-day stay\n"
            "/add VIX,GIG MXP 7-10 — multi-origin, flexible stay"
        )
        return

    from_code = _normalize_airport_arg(context.args[0])
    to_code = _normalize_airport_arg(context.args[1])
    if not from_code or not to_code:
        await update.message.reply_text(
            "Airport codes must be 3 letters (IATA). "
            "Use commas for multiple: VIX,GIG"
        )
        return

    stay_days = None
    stay_days_max = None
    if len(context.args) == 3:
        try:
            stay_days, stay_days_max = parse_stay_range(context.args[2])
        except ValueError:
            await update.message.reply_text("Stay days must be a number or range like 7-10.")
            return
        if stay_days is None:
            await update.message.reply_text("Stay days must be a number or range like 7-10.")
            return
        if stay_days < MIN_STAY_DAYS or (stay_days_max or stay_days) > MAX_STAY_DAYS:
            await update.message.reply_text(
                f"Stay days must be between {MIN_STAY_DAYS} and {MAX_STAY_DAYS}."
            )
            return

    route_id = await db.add_route(
        from_code, to_code, stay_days=stay_days, stay_days_max=stay_days_max
    )
    from bot.main import schedule_scan_jobs

    await schedule_scan_jobs(context.application)
    keyboard = _stops_keyboard(f"stops_newroute:{route_id}")
    if stay_days:
        if stay_days_max and stay_days_max != stay_days:
            route_label = f"{from_code} ⇄ {to_code} ({stay_days}-{stay_days_max} days)"
        else:
            route_label = f"{from_code} ⇄ {to_code} ({stay_days} days)"
    else:
        route_label = f"{from_code} → {to_code}"
    await update.message.reply_text(
        f"✅ Route added: {route_label} (ID: {route_id})\n"
        "Select stops preference for this route:",
        reply_markup=keyboard,
    )


async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    if not context.args or len(context.args) != 1:
        await update.message.reply_text("Usage: /remove <id>\nUse /routes to see route IDs.")
        return

    try:
        route_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Route ID must be a number.")
        return

    removed = await db.remove_route(route_id)
    if removed:
        from bot.main import SCAN_JOB_PREFIX

        for job in context.job_queue.get_jobs_by_name(f"{SCAN_JOB_PREFIX}{route_id}"):
            job.schedule_removal()
        for job in context.job_queue.get_jobs_by_name(f"retry_{route_id}"):
            job.schedule_removal()
        await update.message.reply_text(f"✅ Route {route_id} removed.")
    else:
        await update.message.reply_text(f"❌ Route {route_id} not found.")


def _route_label(route: dict) -> str:
    stay_days = route.get("stay_days")
    stay_max = route.get("stay_days_max")
    if stay_days:
        if stay_max and stay_max != stay_days:
            return f"{route['from_airport']} ⇄ {route['to_airport']} ({stay_days}-{stay_max}d)"
        return f"{route['from_airport']} ⇄ {route['to_airport']} ({stay_days}d)"
    return f"{route['from_airport']} → {route['to_airport']}"


async def routes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    routes = await db.get_active_routes()
    if not routes:
        await update.message.reply_text("No active routes. Use /add to add one.")
        return

    global_pref = await db.get_config("stops_preference") or "any"
    global_interval = await db.get_config("scan_interval") or "1440"
    lines = ["📋 Active Routes:\n"]
    keyboard_rows = []
    for r in routes:
        effective_stops = r["max_stops"] or global_pref
        stops_label = STOPS_LABELS.get(effective_stops, effective_stops)
        effective_interval = r["scan_interval"] or global_interval
        freq_label = INTERVAL_LABELS.get(effective_interval, f"{effective_interval}m")
        alert_bits = []
        if r.get("target_price") is not None:
            alert_bits.append(f"target≤{r['target_price']:g}")
        drop = r.get("alert_drop_pct")
        if drop is not None:
            alert_bits.append(f"drop≥{drop:g}%")
        alert_txt = f" | alerts: {', '.join(alert_bits)}" if alert_bits else ""
        lines.append(
            f"  {r['id']}. {_route_label(r)} | {stops_label} | Every {freq_label}{alert_txt}"
        )
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    f"Change Stops: {r['from_airport']} → {r['to_airport']}",
                    callback_data=f"stops_pick:{r['id']}",
                )
            ]
        )
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    f"Change Frequency: {r['from_airport']} → {r['to_airport']}",
                    callback_data=f"freq_pick:{r['id']}",
                )
            ]
        )

    markup = InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None
    await update.message.reply_text("\n".join(lines), reply_markup=markup)


async def alert_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "Usage:\n"
            "/alert <id> target <price>\n"
            "/alert <id> drop <pct>\n"
            "/alert <id> clear\n"
            f"Defaults: drop {DEFAULT_ALERT_DROP_PCT:g}%, "
            f"cooldown {DEFAULT_ALERT_COOLDOWN_MINUTES}m"
        )
        return

    try:
        route_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Route ID must be a number.")
        return

    action = context.args[1].lower()
    route = await db.get_route(route_id)
    if not route or not route.get("is_active", 1):
        await update.message.reply_text(f"❌ Route {route_id} not found.")
        return

    if action == "clear":
        await db.clear_route_target_price(route_id)
        await update.message.reply_text(f"✅ Cleared target price for route {route_id}.")
        return

    if action == "target":
        if len(context.args) != 3:
            await update.message.reply_text("Usage: /alert <id> target <price>")
            return
        try:
            price = float(context.args[2])
        except ValueError:
            await update.message.reply_text("Price must be a number.")
            return
        await db.set_route_alert(route_id, target_price=price)
        await update.message.reply_text(
            f"✅ Route {route_id} will alert when price ≤ {price:g} {CURRENCY}."
        )
        return

    if action == "drop":
        if len(context.args) != 3:
            await update.message.reply_text("Usage: /alert <id> drop <pct>")
            return
        try:
            pct = float(context.args[2])
        except ValueError:
            await update.message.reply_text("Percent must be a number.")
            return
        await db.set_route_alert(route_id, alert_drop_pct=pct)
        await update.message.reply_text(
            f"✅ Route {route_id} will alert on drops ≥ {pct:g}%."
        )
        return

    await update.message.reply_text("Unknown alert action. Use target, drop, or clear.")


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    routes = await db.get_active_routes()
    if not routes:
        await update.message.reply_text("No active routes. Use /add to add one.")
        return

    await update.message.reply_text("🔍 Scanning... this may take a moment.")

    for route in routes:
        await _scan_and_send(context, route, use_lock=True)


async def time_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    if not context.args or len(context.args) != 1:
        current = await db.get_config("notify_time")
        await update.message.reply_text(
            f"Current scan start time: {current} ({TIMEZONE})\nUsage: /time <HH:MM>"
        )
        return

    time_str = context.args[0]
    try:
        datetime.strptime(time_str, "%H:%M")
    except ValueError:
        await update.message.reply_text("Invalid format. Use HH:MM (e.g. 08:00, 14:30)")
        return

    await db.set_config("notify_time", time_str)
    from bot.main import schedule_scan_jobs

    await schedule_scan_jobs(context.application)
    await update.message.reply_text(f"✅ Scan start time set to {time_str} ({TIMEZONE})")


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    routes = await db.get_active_routes()
    if not routes:
        await update.message.reply_text("No active routes.")
        return

    for route in routes:
        history = await db.get_price_history(route["id"], days=14)
        stats = await db.get_route_price_stats(route["id"], days=30)
        fx_amounts = None
        if stats.get("latest") is not None and fx_service:
            try:
                fx_amounts = await fx_service.convert(stats["latest"], base=CURRENCY)
            except Exception:
                logger.exception("FX convert failed for history")
        msg = format_history_message(
            route["from_airport"],
            route["to_airport"],
            history,
            stay_days=route.get("stay_days"),
            stay_days_max=route.get("stay_days_max"),
            stats=stats,
            fx_amounts=fx_amounts,
            currency=CURRENCY,
        )
        await update.message.reply_text(msg)


async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    await db.set_config("is_paused", "1")
    await update.message.reply_text("⏸ Daily updates paused. Use /resume to restart.")


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    await db.set_config("is_paused", "0")
    await update.message.reply_text("▶️ Daily updates resumed.")


async def stops_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    current = await db.get_config("stops_preference") or "any"
    keyboard = _stops_keyboard("stops_global", current)
    await update.message.reply_text(
        f"Current default stops preference: {STOPS_LABELS.get(current, current)}\n"
        "Select new default:",
        reply_markup=keyboard,
    )


async def frequency_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    current = await db.get_config("scan_interval") or "1440"
    label = INTERVAL_LABELS.get(current, f"{current}m")
    keyboard = _frequency_keyboard("freq_global", current)
    await update.message.reply_text(
        f"Current scan frequency: every {label}\n"
        "Select new frequency:",
        reply_markup=keyboard,
    )


async def stops_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    query = update.callback_query
    await query.answer()

    data = query.data
    if data.startswith("stops_global:"):
        value = data.split(":")[1]
        if value not in STOPS_LABELS:
            return
        await db.set_config("stops_preference", value)
        await query.edit_message_text(f"✅ Default stops preference set to: {STOPS_LABELS[value]}")

    elif data.startswith("stops_route:"):
        parts = data.split(":")
        try:
            route_id = int(parts[1])
        except (ValueError, IndexError):
            return
        value = parts[2] if len(parts) > 2 else None
        if value not in STOPS_LABELS:
            return
        await db.set_route_stops(route_id, value)
        routes = await db.get_active_routes()
        route = next((r for r in routes if r["id"] == route_id), None)
        if route:
            await query.edit_message_text(
                f"✅ {route['from_airport']} → {route['to_airport']} stops set to: {STOPS_LABELS[value]}"
            )
        else:
            await query.edit_message_text(f"✅ Route stops preference updated to: {STOPS_LABELS[value]}")

    elif data.startswith("stops_newroute:"):
        parts = data.split(":")
        try:
            route_id = int(parts[1])
        except (ValueError, IndexError):
            return
        value = parts[2] if len(parts) > 2 else None
        if value not in STOPS_LABELS:
            return
        await db.set_route_stops(route_id, value)
        await query.edit_message_text(f"✅ Stops preference set to: {STOPS_LABELS[value]}")

    elif data.startswith("stops_pick:"):
        try:
            route_id = int(data.split(":")[1])
        except (ValueError, IndexError):
            return
        routes = await db.get_active_routes()
        route = next((r for r in routes if r["id"] == route_id), None)
        if route:
            current = route["max_stops"]
            keyboard = _stops_keyboard(f"stops_route:{route_id}", current)
            await query.edit_message_text(
                f"Select stops preference for {route['from_airport']} → {route['to_airport']}:",
                reply_markup=keyboard,
            )


async def frequency_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    query = update.callback_query
    await query.answer()

    data = query.data
    if data.startswith("freq_global:"):
        value = data.split(":")[1]
        if value not in INTERVAL_LABELS:
            return
        await db.set_config("scan_interval", value)
        from bot.main import schedule_scan_jobs

        await schedule_scan_jobs(context.application)
        label = INTERVAL_LABELS[value]
        await query.edit_message_text(f"✅ Scan frequency set to every {label} for all routes.")

    elif data.startswith("freq_pick:"):
        try:
            route_id = int(data.split(":")[1])
        except (ValueError, IndexError):
            return
        routes = await db.get_active_routes()
        route = next((r for r in routes if r["id"] == route_id), None)
        if route:
            current = route.get("scan_interval")
            keyboard = _frequency_keyboard(f"freq_route:{route_id}", current)
            await query.edit_message_text(
                f"Select scan frequency for {route['from_airport']} → {route['to_airport']}:",
                reply_markup=keyboard,
            )

    elif data.startswith("freq_route:"):
        parts = data.split(":")
        try:
            route_id = int(parts[1])
        except (ValueError, IndexError):
            return
        value = parts[2] if len(parts) > 2 else None
        if value not in INTERVAL_LABELS:
            return
        await db.set_route_scan_interval(route_id, value)
        from bot.main import schedule_scan_jobs

        await schedule_scan_jobs(context.application)
        routes = await db.get_active_routes()
        route = next((r for r in routes if r["id"] == route_id), None)
        label = INTERVAL_LABELS[value]
        if route:
            await query.edit_message_text(
                f"✅ Scan frequency for {route['from_airport']} → {route['to_airport']} set to every {label}."
            )
        else:
            await query.edit_message_text(f"✅ Route scan frequency set to every {label}.")


async def _should_send_summary() -> bool:
    cfg = await db.get_config("always_send_summary")
    if cfg is None:
        return ALWAYS_SEND_SCAN_SUMMARY
    return cfg != "0"


async def _scan_and_send(
    context: ContextTypes.DEFAULT_TYPE,
    route: dict,
    is_retry: bool = False,
    use_lock: bool = False,
):
    """Scan a single route, persist history/alerts, and notify."""
    from_code = route["from_airport"]
    to_code = route["to_airport"]

    if use_lock:
        scanning = context.bot_data.setdefault("_scanning_routes", set())
        if route["id"] in scanning:
            return
        scanning.add(route["id"])

    started = time.monotonic()
    try:
        max_stops = await db.get_route_stops_preference(route["id"])
        stay_days = route.get("stay_days")
        stay_days_max = route.get("stay_days_max")
        if stay_days is not None:
            stay_days = int(stay_days)
        if stay_days_max is not None:
            stay_days_max = int(stay_days_max)

        # Previous cheapest BEFORE this run
        prev_cheapest = await db.get_previous_cheapest(route["id"])
        stats_before = await db.get_route_price_stats(route["id"], days=30)

        result = await scan_route(
            from_code,
            to_code,
            max_stops=max_stops,
            stay_days=stay_days,
            stay_days_max=stay_days_max,
            currency=CURRENCY,
            country=COUNTRY,
        )

        duration_ms = int((time.monotonic() - started) * 1000)

        if result is NO_MATCHES:
            await db.create_scan_run(
                route["id"],
                "no_matches",
                provider="fli",
                currency=CURRENCY,
                duration_ms=duration_ms,
                filters_json=json.dumps({"max_stops": max_stops}),
            )
            stops_label = STOPS_LABELS.get(max_stops, max_stops)
            msg = (
                f"✈️ {_route_label(route)}\n"
                f"No flights found matching filter: {stops_label}\n"
                "Try a less restrictive stops preference via /stops or /routes."
            )
            await context.bot.send_message(chat_id=CHAT_ID, text=msg)
            return

        if result is None:
            await db.create_scan_run(
                route["id"],
                "error",
                provider="fli",
                currency=CURRENCY,
                duration_ms=duration_ms,
                error="scan returned None",
            )
            if is_retry:
                msg = format_retry_failed_message(from_code, to_code, stay_days=stay_days)
                await context.bot.send_message(chat_id=CHAT_ID, text=msg)
            else:
                interval = await db.get_route_scan_interval(route["id"])
                if interval > 240:
                    msg = format_error_message(from_code, to_code, stay_days=stay_days)
                    await context.bot.send_message(chat_id=CHAT_ID, text=msg)
                    # Deduplicate retry jobs
                    existing = context.job_queue.get_jobs_by_name(f"retry_{route['id']}")
                    if not existing:
                        context.job_queue.run_once(
                            _retry_scan_job,
                            when=4 * 60 * 60,
                            data={"route_id": route["id"]},
                            name=f"retry_{route['id']}",
                        )
                else:
                    msg = (
                        f"⚠️ {from_code} → {to_code}\n"
                        "Scan failed. Will retry on next scheduled scan.\n"
                        "Run /check to try manually."
                    )
                    await context.bot.send_message(chat_id=CHAT_ID, text=msg)
            return

        scan_run_id = await db.create_scan_run(
            route["id"],
            "ok",
            provider=result.provider,
            currency=result.currency,
            duration_ms=duration_ms,
            cheapest_price=result.cheapest_price,
            cheapest_travel_date=result.cheapest_travel_date,
            cheapest_return_date=result.cheapest_return_date,
            fare_type=result.fare_type,
            candidates_checked=result.candidates_checked,
            filters_json=json.dumps(
                {
                    "max_stops": max_stops,
                    "stay_days": stay_days,
                    "stay_days_max": stay_days_max,
                }
            ),
        )

        snapshots = []
        for i, day in enumerate(result.top_days):
            snap = dict(day)
            snap["is_cheapest"] = i == 0
            snapshots.append(snap)
        await db.save_fare_snapshots(route["id"], scan_run_id, snapshots, result.currency)

        today = datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d")
        await db.save_price_history(
            route_id=route["id"],
            scan_date=today,
            cheapest_travel_date=result.cheapest_travel_date,
            cheapest_return_date=result.cheapest_return_date,
            cheapest_price=result.cheapest_price,
            cheapest_airline=result.cheapest_airline,
            avg_price=result.avg_price,
            price_data=json.dumps(result.top_days),
            currency=result.currency,
            provider=result.provider,
            fare_type=result.fare_type,
        )

        fx_amounts = None
        if fx_service:
            try:
                fx_amounts = await fx_service.convert(result.cheapest_price, base=result.currency)
            except Exception:
                logger.exception("FX conversion failed")

        alert_hits = await evaluate_alerts(
            db,
            route,
            price=result.cheapest_price,
            travel_date=result.cheapest_travel_date,
            prev_price=prev_cheapest,
            stats=stats_before,
            currency=result.currency,
        )
        alert_lines = []
        for hit in alert_hits:
            recorded = await db.record_alert_event(
                route["id"],
                hit.rule,
                hit.price,
                hit.fingerprint,
                currency=result.currency,
            )
            if recorded:
                alert_lines.append(hit.message)

        stops_label = STOPS_LABELS.get(max_stops) if max_stops != "any" else None
        send_summary = await _should_send_summary()

        if send_summary:
            msg = format_daily_message(
                result,
                prev_cheapest=prev_cheapest,
                stops_label=stops_label,
                max_stops=max_stops,
                fx_amounts=fx_amounts,
                alert_lines=alert_lines or None,
                stats=stats_before if stats_before.get("count") else None,
            )
            await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")
        elif alert_lines:
            msg = format_alert_only_message(result, alert_lines, fx_amounts=fx_amounts)
            await context.bot.send_message(chat_id=CHAT_ID, text=msg)
    finally:
        if use_lock:
            scanning = context.bot_data.get("_scanning_routes", set())
            scanning.discard(route["id"])


async def _retry_scan_job(context: ContextTypes.DEFAULT_TYPE):
    is_paused = await db.get_config("is_paused")
    if is_paused == "1":
        return

    data = context.job.data or {}
    if isinstance(data, dict) and "route_id" in data:
        route = await db.get_route(data["route_id"])
    elif isinstance(data, dict) and "id" in data:
        route = await db.get_route(data["id"])
    else:
        return

    if not route or not route.get("is_active"):
        return

    await _scan_and_send(context, route, is_retry=True, use_lock=True)


async def _scheduled_scan_route(context: ContextTypes.DEFAULT_TYPE):
    is_paused = await db.get_config("is_paused")
    if is_paused == "1":
        return

    route = context.job.data
    if not route:
        return

    # Refresh route in case settings changed
    fresh = await db.get_route(route["id"])
    if not fresh or not fresh.get("is_active"):
        return

    await _scan_and_send(context, fresh, use_lock=True)

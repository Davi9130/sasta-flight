import json
import logging
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.config import CHAT_ID, INTERVAL_OPTIONS, MAX_STAY_DAYS, MIN_STAY_DAYS
from bot.db import Database
from bot.scanner import scan_route, NO_MATCHES
from bot.formatter import (
    format_daily_message,
    format_error_message,
    format_history_message,
    format_retry_failed_message,
)

logger = logging.getLogger(__name__)

# Global db reference, set in main.py
db: Database = None

STOPS_LABELS = {
    "any": "Any",
    "direct": "Direct",
    "1stop": "Up to 1 Stop",
    "2stops": "Up to 2 Stops",
}

INTERVAL_LABELS = {str(v): k for k, v in INTERVAL_OPTIONS.items()}  # {"60": "1h", ...}


def _frequency_keyboard(callback_prefix: str, current_minutes: str | None = None) -> InlineKeyboardMarkup:
    """Build inline keyboard for frequency selection."""
    buttons = []
    for label, minutes in INTERVAL_OPTIONS.items():
        display = f">> {label} <<" if str(minutes) == current_minutes else label
        buttons.append(InlineKeyboardButton(display, callback_data=f"{callback_prefix}:{minutes}"))
    return InlineKeyboardMarkup([buttons[:3], buttons[3:]])


def _stops_keyboard(callback_prefix: str, current: str | None = None) -> InlineKeyboardMarkup:
    """Build inline keyboard for stops selection."""
    buttons = []
    for value, label in STOPS_LABELS.items():
        display = f">> {label} <<" if value == current else label
        buttons.append(InlineKeyboardButton(display, callback_data=f"{callback_prefix}:{value}"))
    return InlineKeyboardMarkup([buttons])


def _is_authorized(update: Update) -> bool:
    return update.effective_chat.id == CHAT_ID


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    await update.message.reply_text(
        "✈️ SastaFlight - Flight Price Scanner\n\n"
        "Commands:\n"
        "/add <from> <to> [days] - Add a route (e.g. /add VIX MXP 10)\n"
        "/remove <id> - Remove a route\n"
        "/routes - List active routes\n"
        "/stops - Set default stops preference\n"
        "/frequency - Set scan frequency\n"
        "/check - Scan all routes now\n"
        "/time <HH:MM> - Set scan start time (24h, IST)\n"
        "/history - 7-day price trend\n"
        "/pause - Pause scheduled scans\n"
        "/resume - Resume scheduled scans\n"
        "/help - Show this message"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    await start_command(update, context)


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    if not context.args or len(context.args) not in (2, 3):
        await update.message.reply_text(
            "Usage: /add <from> <to> [days]\n"
            "Examples:\n"
            "/add ATQ BOM — one-way\n"
            "/add VIX MXP 10 — round-trip, 10-day stay"
        )
        return

    from_code = context.args[0].upper()
    to_code = context.args[1].upper()

    if len(from_code) != 3 or len(to_code) != 3:
        await update.message.reply_text("Airport codes must be 3 letters (IATA codes).")
        return

    stay_days = None
    if len(context.args) == 3:
        try:
            stay_days = int(context.args[2])
        except ValueError:
            await update.message.reply_text("Stay days must be a whole number.")
            return
        if stay_days < MIN_STAY_DAYS or stay_days > MAX_STAY_DAYS:
            await update.message.reply_text(
                f"Stay days must be between {MIN_STAY_DAYS} and {MAX_STAY_DAYS}."
            )
            return

    route_id = await db.add_route(from_code, to_code, stay_days=stay_days)
    # Schedule a scan job for the new route
    from bot.main import schedule_scan_jobs
    await schedule_scan_jobs(context.application)
    keyboard = _stops_keyboard(f"stops_newroute:{route_id}")
    if stay_days:
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
        # Cancel the scheduled scan job for this route
        from bot.main import SCAN_JOB_PREFIX
        for job in context.job_queue.get_jobs_by_name(f"{SCAN_JOB_PREFIX}{route_id}"):
            job.schedule_removal()
        await update.message.reply_text(f"✅ Route {route_id} removed.")
    else:
        await update.message.reply_text(f"❌ Route {route_id} not found.")


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
        stay_days = r.get("stay_days")
        if stay_days:
            route_label = f"{r['from_airport']} ⇄ {r['to_airport']} ({stay_days}d)"
        else:
            route_label = f"{r['from_airport']} → {r['to_airport']}"
        lines.append(f"  {r['id']}. {route_label} | {stops_label} | Every {freq_label}")
        keyboard_rows.append([
            InlineKeyboardButton(
                f"Change Stops: {r['from_airport']} → {r['to_airport']}",
                callback_data=f"stops_pick:{r['id']}",
            )
        ])
        keyboard_rows.append([
            InlineKeyboardButton(
                f"Change Frequency: {r['from_airport']} → {r['to_airport']}",
                callback_data=f"freq_pick:{r['id']}",
            )
        ])

    markup = InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None
    await update.message.reply_text("\n".join(lines), reply_markup=markup)


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    routes = await db.get_active_routes()
    if not routes:
        await update.message.reply_text("No active routes. Use /add to add one.")
        return

    await update.message.reply_text("🔍 Scanning... this may take a moment.")

    for route in routes:
        await _scan_and_send(context, route)


async def time_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    if not context.args or len(context.args) != 1:
        current = await db.get_config("notify_time")
        await update.message.reply_text(
            f"Current scan start time: {current} IST\nUsage: /time <HH:MM>"
        )
        return

    time_str = context.args[0]
    try:
        datetime.strptime(time_str, "%H:%M")
    except ValueError:
        await update.message.reply_text("Invalid format. Use HH:MM (e.g. 08:00, 14:30)")
        return

    await db.set_config("notify_time", time_str)

    # Reschedule - import here to avoid circular
    from bot.main import schedule_scan_jobs
    await schedule_scan_jobs(context.application)

    await update.message.reply_text(f"✅ Scan start time set to {time_str} IST")


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    routes = await db.get_active_routes()
    if not routes:
        await update.message.reply_text("No active routes.")
        return

    for route in routes:
        history = await db.get_price_history(route["id"], days=7)
        msg = format_history_message(
            route["from_airport"],
            route["to_airport"],
            history,
            stay_days=route.get("stay_days"),
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
    """Handle inline keyboard callbacks for stops preference."""
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
    """Handle inline keyboard callbacks for frequency."""
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


async def _scan_and_send(context: ContextTypes.DEFAULT_TYPE, route: dict, is_retry: bool = False):
    """Scan a single route and send the result. Schedule retry on failure."""
    from_code = route["from_airport"]
    to_code = route["to_airport"]

    # Resolve stops preference
    max_stops = await db.get_route_stops_preference(route["id"])

    stay_days = route.get("stay_days")
    if stay_days is not None:
        stay_days = int(stay_days)

    result = await scan_route(from_code, to_code, max_stops=max_stops, stay_days=stay_days)

    if result is NO_MATCHES:
        stops_label = STOPS_LABELS.get(max_stops, max_stops)
        route_label = f"{from_code} ⇄ {to_code}" if stay_days else f"{from_code} → {to_code}"
        msg = (
            f"✈️ {route_label}\n"
            f"No flights found matching filter: {stops_label}\n"
            "Try a less restrictive stops preference via /stops or /routes."
        )
        await context.bot.send_message(chat_id=CHAT_ID, text=msg)
        return

    if result is None:
        if is_retry:
            msg = format_retry_failed_message(from_code, to_code, stay_days=stay_days)
            await context.bot.send_message(chat_id=CHAT_ID, text=msg)
        else:
            # Schedule retry only if interval > 4 hours
            interval = await db.get_route_scan_interval(route["id"])
            if interval > 240:
                msg = format_error_message(from_code, to_code, stay_days=stay_days)
                await context.bot.send_message(chat_id=CHAT_ID, text=msg)
                context.job_queue.run_once(
                    _retry_scan_job,
                    when=4 * 60 * 60,
                    data=route,
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

    # Get previous cheapest for trend
    history = await db.get_price_history(route["id"], days=1)
    prev_cheapest = history[0]["cheapest_price"] if history else None

    # Save to history
    today = datetime.now().strftime("%Y-%m-%d")
    await db.save_price_history(
        route_id=route["id"],
        scan_date=today,
        cheapest_travel_date=result.cheapest_travel_date,
        cheapest_return_date=result.cheapest_return_date,
        cheapest_price=result.cheapest_price,
        cheapest_airline=result.cheapest_airline,
        avg_price=result.avg_price,
        price_data=json.dumps(result.top_days),
    )

    stops_label = STOPS_LABELS.get(max_stops) if max_stops != "any" else None
    msg = format_daily_message(result, prev_cheapest=prev_cheapest, stops_label=stops_label, max_stops=max_stops)
    await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")


async def _retry_scan_job(context: ContextTypes.DEFAULT_TYPE):
    """Retry a failed scan (called by JobQueue)."""
    route = context.job.data
    await _scan_and_send(context, route, is_retry=True)


async def _scheduled_scan_route(context: ContextTypes.DEFAULT_TYPE):
    """Repeating job callback for a single route."""
    is_paused = await db.get_config("is_paused")
    if is_paused == "1":
        return

    route = context.job.data
    if not route:
        return

    scanning = context.bot_data.setdefault("_scanning_routes", set())
    if route["id"] in scanning:
        return
    scanning.add(route["id"])
    try:
        await _scan_and_send(context, route)
    finally:
        scanning.discard(route["id"])

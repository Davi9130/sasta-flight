"""Telegram commands and inline-button callbacks."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from bot.config import (
    CHAT_ID,
    CURRENCY,
    DEFAULT_ALERT_COOLDOWN_MINUTES,
    INTERVAL_OPTIONS,
    MAX_HUB_NIGHTS,
    MAX_STAY_DAYS,
    MIN_STAY_DAYS,
    TIMEZONE,
)
from bot.formatter import (
    format_deferred_message,
    format_guard_status,
    format_history_message,
    format_via_message,
    route_label,
)
from bot.runner import ALERT, DEFERRED, ERROR, NO_MATCH, STOPS_LABELS
from bot.scheduler import cancel_route_jobs, schedule_scan_jobs
from bot.search.hubs import ViaRequest, parse_via_args, scan_via_route
from bot.search.scan import NO_MATCHES, ScanDeferred, parse_airport_list, parse_stay_range
from bot.services import Services, get_services

logger = logging.getLogger(__name__)

INTERVAL_LABELS = {str(v): k for k, v in INTERVAL_OPTIONS.items()}
VIA_PENDING_KEY = "_via_pending"


def _svc(context: ContextTypes.DEFAULT_TYPE) -> Services:
    return get_services(context.application)


def _is_authorized(update: Update) -> bool:
    return update.effective_chat is not None and update.effective_chat.id == CHAT_ID


def _frequency_keyboard(prefix: str, current: str | None = None) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            f">> {label} <<" if str(minutes) == current else label, callback_data=f"{prefix}:{minutes}"
        )
        for label, minutes in INTERVAL_OPTIONS.items()
    ]
    return InlineKeyboardMarkup([buttons[:3], buttons[3:]])


def _stops_keyboard(prefix: str, current: str | None = None) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(f">> {label} <<" if value == current else label, callback_data=f"{prefix}:{value}")
        for value, label in STOPS_LABELS.items()
    ]
    return InlineKeyboardMarkup([buttons])


def _help_text() -> str:
    return (
        "✈️ SastaFlight - Flight Price Scanner\n\n"
        "Commands:\n"
        "/add <from> <to> [days|min-max] - Add a route\n"
        "  /add VIX MXP — one-way\n"
        "  /add VIX,GIG MXP,BGY 10 — multi-airport, 10-day stay\n"
        "  /add VIX MXP 7-10 — flexible stay range\n"
        "/via <from> <to> <stay> [hub nights] [save]\n"
        "  Separate tickets via São Paulo or Rio\n"
        "  /via VIX MXP 7-10 0-2\n"
        "  /via VIX MXP 10 1 save\n"
        "/remove <id> - Remove a route\n"
        "/routes - List active routes\n"
        "/stops - Set default stops preference\n"
        "/frequency - Set scan frequency\n"
        "/alert <id> target <price> - Notify only when price ≤ target\n"
        "/alert <id> drop <pct> - Optional: also alert on % drop\n"
        "/alert <id> low on|off - Optional: alert on new historical low\n"
        "/alert <id> clear - Clear alert rules\n"
        "/check - Scan all routes now (silent unless target hit)\n"
        f"/time <HH:MM> - Set scan start time (24h, {TIMEZONE})\n"
        "/history - Price trend + stats\n"
        "/status - Google request budget and block status\n"
        "/pause - Pause scheduled scans\n"
        "/resume - Resume scheduled scans\n"
        "/help - Show this message\n\n"
        f"Prices tracked in {CURRENCY} (≈ EUR / USD when available).\n"
        "Scans stay silent and only notify when a route hits its target.\n"
        "Google requests are budgeted and paced to avoid blocks."
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    await update.message.reply_text(_help_text())


help_command = start_command


def _normalize_airport_arg(raw: str) -> str | None:
    codes = parse_airport_list(raw)
    if not codes or any(len(c) != 3 or not c.isalpha() for c in codes):
        return None
    return ",".join(codes)


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    args = context.args or []
    if len(args) not in (2, 3):
        await update.message.reply_text(
            "Usage: /add <from> <to> [days|min-max]\n"
            "Examples:\n"
            "/add ATQ BOM — one-way\n"
            "/add VIX MXP 10 — round-trip, 10-day stay\n"
            "/add VIX,GIG MXP 7-10 — multi-origin, flexible stay"
        )
        return
    from_code, to_code = _normalize_airport_arg(args[0]), _normalize_airport_arg(args[1])
    if not from_code or not to_code:
        await update.message.reply_text("Airport codes must be 3 letters (IATA). Use commas for multiple: VIX,GIG")
        return

    stay_min = stay_max = None
    if len(args) == 3:
        try:
            stay_min, stay_max = parse_stay_range(args[2])
        except ValueError:
            stay_min = None
        if stay_min is None:
            await update.message.reply_text("Stay days must be a number or range like 7-10.")
            return
        if stay_min < MIN_STAY_DAYS or stay_max > MAX_STAY_DAYS:
            await update.message.reply_text(f"Stay days must be between {MIN_STAY_DAYS} and {MAX_STAY_DAYS}.")
            return

    svc = _svc(context)
    route_id = await svc.db.add_route(from_code, to_code, stay_min=stay_min, stay_max=stay_max)
    await schedule_scan_jobs(context.application)
    route = await svc.db.get_route(route_id)
    await update.message.reply_text(
        f"✅ Route added: {route_label(route)} (ID: {route_id})\nSelect stops preference for this route:",
        reply_markup=_stops_keyboard(f"stops_newroute:{route_id}"),
    )


def _via_usage() -> str:
    return (
        "Usage: /via <from> <to> <stay> [hub nights] [save]\n"
        "Examples:\n"
        "/via VIX MXP 7-10 0-2 — round trip via SP/RJ, 0-2 nights at the hub\n"
        "/via VIX MXP 10 1 — exactly 1 night in SP or Rio each way\n"
        "/via VIX,GIG MXP 7-10 0-2 save — search and monitor"
    )


async def _save_via_route(svc: Services, request: ViaRequest) -> int:
    return await svc.db.add_route(
        ",".join(request.origins),
        ",".join(request.destinations),
        stay_min=request.stay_min,
        stay_max=request.stay_max if request.stay_max != request.stay_min else None,
        via_hub=True,
        hub_nights_min=request.hub_nights_min,
        hub_nights_max=request.hub_nights_max if request.hub_nights_max != request.hub_nights_min else None,
    )


async def via_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    try:
        request = parse_via_args(context.args or [])
    except ValueError as exc:
        messages = {
            "stay": f"Stay at the destination must be between {MIN_STAY_DAYS} and {MAX_STAY_DAYS} days.",
            "hub": f"Hub nights must be between 0 and {MAX_HUB_NIGHTS} (example: 0-2).",
            "airports": "Airport codes must be 3 letters (IATA). Use commas for multiple: VIX,GIG",
        }
        await update.message.reply_text(messages.get(str(exc), _via_usage()))
        return

    await update.message.reply_text(
        f"Buscando {','.join(request.origins)} ⇄ {','.join(request.destinations)} via SP/RJ… "
        "(pode levar alguns minutos: as buscas são espaçadas para evitar bloqueio)"
    )
    context.application.create_task(_run_via(update, context, request))


async def _run_via(update: Update, context: ContextTypes.DEFAULT_TYPE, request: ViaRequest):
    svc = _svc(context)
    stops = await svc.db.get_config("stops_preference") or "any"
    label = f"{','.join(request.origins)} ⇄ {','.join(request.destinations)} via SP/RJ"
    try:
        result = await scan_via_route(
            svc.fares,
            origins=request.origins,
            destinations=request.destinations,
            stay_min=request.stay_min,
            stay_max=request.stay_max,
            hub_nights_min=request.hub_nights_min,
            hub_nights_max=request.hub_nights_max,
            max_stops=stops,
            currency=CURRENCY,
        )
    except ScanDeferred as exc:
        await update.message.reply_text(format_deferred_message(label, exc.reason, exc.retry_after))
        return
    except Exception:
        logger.exception("/via failed")
        await update.message.reply_text("❌ A busca via SP/RJ falhou. Tente de novo mais tarde.")
        return
    if result is NO_MATCHES:
        await update.message.reply_text(
            "Nenhuma combinação via SP/RJ encontrada nessa janela. "
            "Tente mais noites no hub ou uma estadia diferente."
        )
        return

    fx_amounts = None
    if svc.fx:
        try:
            fx_amounts = await svc.fx.convert(result.cheapest_price, base=result.currency)
        except Exception:
            logger.exception("FX conversion failed")
    msg = format_via_message(result, max_stops=stops, fx_amounts=fx_amounts)

    markup = None
    if request.save:
        route_id = await _save_via_route(svc, request)
        await schedule_scan_jobs(context.application)
        msg += f"\n\n✅ Monitorando (ID: {route_id}). Alerta com /alert {route_id} target <preço>."
    else:
        token = str(time.time_ns())
        context.bot_data.setdefault(VIA_PENDING_KEY, {})[token] = request
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Monitorar esta rota", callback_data=f"via_save:{token}")]]
        )
    await update.message.reply_text(
        msg, parse_mode="Markdown", reply_markup=markup, disable_web_page_preview=True
    )


async def via_save_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not _is_authorized(update):
        return
    token = query.data.split(":", 1)[1]
    request = context.bot_data.get(VIA_PENDING_KEY, {}).pop(token, None)
    await query.edit_message_reply_markup(reply_markup=None)
    if request is None:
        await query.message.reply_text("Essa busca expirou. Rode /via de novo.")
        return
    route_id = await _save_via_route(_svc(context), request)
    await schedule_scan_jobs(context.application)
    await query.message.reply_text(f"✅ Monitorando (ID: {route_id}). Alerta com /alert {route_id} target <preço>.")


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
    if await _svc(context).db.remove_route(route_id):
        cancel_route_jobs(context.application, route_id)
        await update.message.reply_text(f"✅ Route {route_id} removed.")
    else:
        await update.message.reply_text(f"❌ Route {route_id} not found.")


async def routes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    db = _svc(context).db
    routes = await db.get_active_routes()
    if not routes:
        await update.message.reply_text("No active routes. Use /add to add one.")
        return
    global_stops = await db.get_config("stops_preference") or "any"
    global_interval = await db.get_config("scan_interval") or "1440"
    lines = ["📋 Active Routes:\n"]
    rows = []
    for r in routes:
        stops = STOPS_LABELS.get(r["max_stops"] or global_stops, r["max_stops"] or global_stops)
        interval = r["scan_interval"] or global_interval
        freq = INTERVAL_LABELS.get(interval, f"{interval}m")
        bits = [f"target≤{r['target_price']:g}" if r.get("target_price") is not None else "silent"]
        if r.get("alert_drop_pct") is not None:
            bits.append(f"drop≥{r['alert_drop_pct']:g}%")
        if r.get("alert_on_new_low") == 1:
            bits.append("new-low")
        lines.append(f"  {r['id']}. {route_label(r)} | {stops} | Every {freq} | {', '.join(bits)}")
        short = f"{r['from_airport']} → {r['to_airport']}"
        rows.append([InlineKeyboardButton(f"Change Stops: {short}", callback_data=f"stops_pick:{r['id']}")])
        rows.append([InlineKeyboardButton(f"Change Frequency: {short}", callback_data=f"freq_pick:{r['id']}")])
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))


async def alert_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Usage:\n"
            "/alert <id> target <price> — notify only when ≤ price\n"
            "/alert <id> drop <pct> — optional extra rule\n"
            "/alert <id> low on|off — alert on new historical low\n"
            "/alert <id> clear — clear alert rules\n"
            "Scans keep saving history; Telegram alerts only on rules.\n"
            f"Cooldown: {DEFAULT_ALERT_COOLDOWN_MINUTES}m"
        )
        return
    try:
        route_id = int(args[0])
    except ValueError:
        await update.message.reply_text("Route ID must be a number.")
        return
    db = _svc(context).db
    route = await db.get_route(route_id)
    if not route or not route.get("is_active"):
        await update.message.reply_text(f"❌ Route {route_id} not found.")
        return

    action = args[1].lower()
    if action == "clear":
        await db.clear_route_alerts(route_id)
        await update.message.reply_text(
            f"✅ Cleared alert rules for route {route_id}. Scans stay silent until you set a new target."
        )
        return
    if action in ("target", "drop") and len(args) == 3:
        try:
            value = float(args[2])
        except ValueError:
            await update.message.reply_text("Value must be a number.")
            return
        if action == "target":
            await db.set_route_alert(route_id, target_price=value)
            await update.message.reply_text(
                f"✅ Route {route_id}: only notify when price ≤ {value:g} {CURRENCY}.\n"
                "History keeps being saved on every scan."
            )
        else:
            await db.set_route_alert(route_id, alert_drop_pct=value)
            await update.message.reply_text(f"✅ Route {route_id} will also alert on drops ≥ {value:g}%.")
        return
    if action == "low" and len(args) == 3 and args[2].lower() in ("on", "off"):
        on = args[2].lower() == "on"
        await db.set_route_alert(route_id, alert_on_new_low=1 if on else 0)
        await update.message.reply_text(
            f"✅ Route {route_id}: new-low alerts {'enabled' if on else 'disabled'}."
        )
        return
    await update.message.reply_text("Unknown alert action. Use target, drop, low, or clear.")


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    svc = _svc(context)
    routes = await svc.db.get_active_routes()
    if not routes:
        await update.message.reply_text("No active routes. Use /add to add one.")
        return
    reason, remaining = await svc.fares.guard.circuit()
    if reason:
        await update.message.reply_text(format_deferred_message("All routes", reason, remaining))
        return
    futures = [svc.queue.enqueue(r["id"], manual=True) for r in routes]
    await update.message.reply_text(
        f"🔍 Scanning {len(routes)} route(s)… requests are spaced out, so this can take a few minutes."
    )
    context.application.create_task(_report_check(update, futures))


async def _report_check(update: Update, futures: list[asyncio.Future]):
    outcomes = await asyncio.gather(*futures)
    counts = {k: outcomes.count(k) for k in (ALERT, DEFERRED, NO_MATCH, ERROR)}
    if counts[ALERT]:
        return
    parts = [f"{len(outcomes)} verificada(s); histórico salvo"]
    if counts[DEFERRED]:
        parts.append(f"{counts[DEFERRED]} adiada(s) por proteção anti-bloqueio")
    if counts[ERROR]:
        parts.append(f"{counts[ERROR]} com erro")
    await update.message.reply_text(f"✅ Scan done. Nenhuma rota no target ({'; '.join(parts)}).")


async def time_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    db = _svc(context).db
    if not context.args or len(context.args) != 1:
        current = await db.get_config("notify_time")
        await update.message.reply_text(f"Current scan start time: {current} ({TIMEZONE})\nUsage: /time <HH:MM>")
        return
    value = context.args[0]
    try:
        datetime.strptime(value, "%H:%M")
    except ValueError:
        await update.message.reply_text("Invalid format. Use HH:MM (e.g. 08:00, 14:30)")
        return
    await db.set_config("notify_time", value)
    await schedule_scan_jobs(context.application)
    await update.message.reply_text(f"✅ Scan start time set to {value} ({TIMEZONE})")


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    svc = _svc(context)
    routes = await svc.db.get_active_routes()
    if not routes:
        await update.message.reply_text("No active routes.")
        return
    for route in routes:
        history = await svc.db.get_price_history(route["id"], days=14)
        stats = await svc.db.get_route_price_stats(route["id"], days=30)
        fx_amounts = None
        if stats.get("latest") is not None and svc.fx:
            try:
                fx_amounts = await svc.fx.convert(stats["latest"], base=CURRENCY)
            except Exception:
                logger.exception("FX convert failed for history")
        await update.message.reply_text(
            format_history_message(route, history, stats=stats, fx_amounts=fx_amounts, currency=CURRENCY)
        )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    svc = _svc(context)
    text = format_guard_status(await svc.fares.guard.snapshot())
    if svc.queue is not None:
        text += f"\nQueue: {svc.queue.size} waiting" + (
            f", scanning route {svc.queue.current}" if svc.queue.current else ""
        )
    await update.message.reply_text(text)


async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    await _svc(context).db.set_config("is_paused", "1")
    await update.message.reply_text("⏸ Scheduled scans paused. Use /resume to restart.")


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    await _svc(context).db.set_config("is_paused", "0")
    await update.message.reply_text("▶️ Scheduled scans resumed.")


async def stops_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    current = await _svc(context).db.get_config("stops_preference") or "any"
    await update.message.reply_text(
        f"Current default stops preference: {STOPS_LABELS.get(current, current)}\nSelect new default:",
        reply_markup=_stops_keyboard("stops_global", current),
    )


async def frequency_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    current = await _svc(context).db.get_config("scan_interval") or "1440"
    await update.message.reply_text(
        f"Current scan frequency: every {INTERVAL_LABELS.get(current, current + 'm')}\nSelect new frequency:",
        reply_markup=_frequency_keyboard("freq_global", current),
    )


def _route_id(part: str) -> int | None:
    try:
        return int(part)
    except (TypeError, ValueError):
        return None


async def stops_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    query = update.callback_query
    await query.answer()
    db = _svc(context).db
    parts = query.data.split(":")
    kind = parts[0]

    if kind == "stops_global" and len(parts) == 2 and parts[1] in STOPS_LABELS:
        await db.set_config("stops_preference", parts[1])
        await query.edit_message_text(f"✅ Default stops preference set to: {STOPS_LABELS[parts[1]]}")
    elif kind in ("stops_route", "stops_newroute") and len(parts) == 3 and parts[2] in STOPS_LABELS:
        route_id = _route_id(parts[1])
        if route_id is None:
            return
        await db.set_route_stops(route_id, parts[2])
        route = await db.get_route(route_id)
        prefix = f"{route['from_airport']} → {route['to_airport']} stops" if route else "Stops preference"
        await query.edit_message_text(f"✅ {prefix} set to: {STOPS_LABELS[parts[2]]}")
    elif kind == "stops_pick" and len(parts) == 2:
        route_id = _route_id(parts[1])
        route = await db.get_route(route_id) if route_id is not None else None
        if route:
            await query.edit_message_text(
                f"Select stops preference for {route['from_airport']} → {route['to_airport']}:",
                reply_markup=_stops_keyboard(f"stops_route:{route_id}", route["max_stops"]),
            )


async def frequency_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_authorized(update):
        return
    query = update.callback_query
    await query.answer()
    db = _svc(context).db
    parts = query.data.split(":")
    kind = parts[0]

    if kind == "freq_global" and len(parts) == 2 and parts[1] in INTERVAL_LABELS:
        await db.set_config("scan_interval", parts[1])
        await schedule_scan_jobs(context.application)
        await query.edit_message_text(f"✅ Scan frequency set to every {INTERVAL_LABELS[parts[1]]} for all routes.")
    elif kind == "freq_pick" and len(parts) == 2:
        route_id = _route_id(parts[1])
        route = await db.get_route(route_id) if route_id is not None else None
        if route:
            await query.edit_message_text(
                f"Select scan frequency for {route['from_airport']} → {route['to_airport']}:",
                reply_markup=_frequency_keyboard(f"freq_route:{route_id}", route.get("scan_interval")),
            )
    elif kind == "freq_route" and len(parts) == 3 and parts[2] in INTERVAL_LABELS:
        route_id = _route_id(parts[1])
        if route_id is None:
            return
        await db.set_route_scan_interval(route_id, parts[2])
        await schedule_scan_jobs(context.application)
        route = await db.get_route(route_id)
        label = INTERVAL_LABELS[parts[2]]
        if route:
            await query.edit_message_text(
                f"✅ Scan frequency for {route['from_airport']} → {route['to_airport']} set to every {label}."
            )
        else:
            await query.edit_message_text(f"✅ Route scan frequency set to every {label}.")

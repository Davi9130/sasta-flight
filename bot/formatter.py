"""Telegram message text. Messages with links use legacy Markdown."""

from __future__ import annotations

from datetime import datetime

from bot.config import CURRENCY, CURRENCY_SYMBOL, DAYS_TO_SCAN, DISPLAY_CURRENCIES
from bot.fares.client import booking_url
from bot.search.scan import ScanResult

SEPARATOR = "━━━━━━━━━━━━━━━━━━━━━━"

DEFER_REASONS = {
    "http_429": "Google rate-limited this server (HTTP 429)",
    "http_403": "Google refused this server (HTTP 403)",
    "http_503": "Google returned HTTP 503 (usually a block)",
    "captcha": "Google asked for a captcha (unusual traffic)",
    "consent": "Google served a consent page instead of results",
    "no_data_script": "Google served a page without flight data (soft block)",
    "budget_hour": "hourly request budget reached",
    "budget_day": "daily request budget reached",
    "proxies_blocked": "every proxy is in quarantine",
}


def md(text: str | None) -> str:
    """Escape user/airline text for legacy Markdown."""
    if not text:
        return ""
    for ch in ("\\", "_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text


def format_price(price: float, currency: str | None = None) -> str:
    code = currency or CURRENCY
    return f"{CURRENCY_SYMBOL.get(code, code + ' ')}{price:,.0f}"


def format_price_multi(
    price: float, currency: str | None = None, fx_amounts: dict[str, float] | None = None
) -> str:
    primary = format_price(price, currency)
    base = currency or CURRENCY
    extras = [
        format_price(fx_amounts[code], code)
        for code in DISPLAY_CURRENCIES
        if fx_amounts and code != base and code in fx_amounts
    ]
    return f"{primary} (≈ {' / '.join(extras)})" if extras else primary


def format_date(value: str) -> str:
    return datetime.strptime(value, "%Y-%m-%d").strftime("%b %d (%a)")


def format_trip_dates(outbound: str, inbound: str | None = None) -> str:
    return f"{format_date(outbound)} → {format_date(inbound)}" if inbound else format_date(outbound)


def format_duration(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    return f"{h}h {m}m"


def format_stops(stops: int) -> str:
    if stops == 0:
        return "Nonstop"
    return f"{stops} stop{'s' if stops > 1 else ''}"


def stay_label(stay_min: int | None, stay_max: int | None, unit: str = "d") -> str:
    if stay_max and stay_max != stay_min:
        return f"{stay_min}-{stay_max}{unit}"
    return f"{stay_min}{unit}"


def route_label(route: dict) -> str:
    f, t = route["from_airport"], route["to_airport"]
    stay_min, stay_max = route.get("stay_min"), route.get("stay_max")
    if route.get("via_hub"):
        hub_min = route.get("hub_nights_min") or 0
        hub_max = route.get("hub_nights_max")
        hub_txt = f"hub {stay_label(hub_min, hub_max, 'n')}"
        return f"{f} ⇄ {t} via SP/RJ ({stay_label(stay_min, stay_max)}, {hub_txt})"
    if stay_min:
        return f"{f} ⇄ {t} ({stay_label(stay_min, stay_max)})"
    return f"{f} → {t}"


def _result_label(result: ScanResult) -> str:
    arrow = "⇄" if result.stay_days is not None else "→"
    return f"{result.from_airport} {arrow} {result.to_airport}"


def _day_url(day: dict, max_stops: str, currency: str) -> str:
    return day.get("booking_url") or booking_url(
        day["from_airport"],
        day["to_airport"],
        day["date"],
        day.get("return_date"),
        max_stops=max_stops,
        currency=currency,
    )


def _book_links(day: dict, max_stops: str, currency: str) -> str:
    if day.get("fare_type") == "split":
        return f"[Out →]({day['out_url']}) · [Return →]({day['in_url']})"
    return f"[Book →]({_day_url(day, max_stops, currency)})"


def _trend_line(price: float, prev: float | None) -> str | None:
    if prev is None or prev <= 0:
        return None
    pct = ((price - prev) / prev) * 100
    if pct < 0:
        return f"💡 Trend: Prices dropped {abs(pct):.0f}% since last scan"
    if pct > 0:
        return f"💡 Trend: Prices rose {pct:.0f}% since last scan"
    return "💡 Trend: Prices unchanged since last scan"


def format_daily_message(
    result: ScanResult,
    *,
    prev_cheapest: float | None = None,
    stops_label: str | None = None,
    max_stops: str = "any",
    fx_amounts: dict[str, float] | None = None,
    alert_lines: list[str] | None = None,
    stats: dict | None = None,
) -> str:
    cur = result.currency
    header = f"✈️ {_result_label(result)} | Next {DAYS_TO_SCAN} Days"
    if result.stay_days is not None:
        header += f" | {stay_label(result.stay_days, result.stay_days_max, '-day stay')}"
    if stops_label:
        header += f" | Filter: {stops_label}"
    lines = [header, SEPARATOR, ""]
    if alert_lines:
        lines += [*alert_lines, ""]

    best = result.cheapest
    lines.append(
        f"🏆 Cheapest: {format_trip_dates(best['date'], best.get('return_date'))} - "
        f"{format_price_multi(best['price'], cur, fx_amounts)}"
    )
    if (best.get("from_airport"), best.get("to_airport")) != (
        result.from_airports[0],
        result.to_airports[0],
    ):
        lines.append(f"   Airports: {best.get('from_airport')} → {best.get('to_airport')}")
    if best.get("fare_type") == "split":
        lines.append(
            f"   ⚠️ Separate tickets (out {format_price(best['outbound_price'], cur)}"
            f" + return {format_price(best['inbound_price'], cur)})"
        )
    details = [md(best.get("airline"))] if best.get("airline") else []
    if details:
        if best.get("departure"):
            details.append(best["departure"])
        if best.get("duration") is not None:
            details.append(format_duration(best["duration"]))
        if best.get("stops") is not None:
            details.append(format_stops(best["stops"]))
        lines.append(f"   {' | '.join(details)}")

    lines += ["", f"📊 Top {len(result.top_days)} Cheapest Days:"]
    for i, day in enumerate(result.top_days, 1):
        note = ""
        if (day.get("from_airport"), day.get("to_airport")) != (
            result.from_airports[0],
            result.to_airports[0],
        ):
            note = f" ({day['from_airport']}→{day['to_airport']})"
        suffix = " · split tickets" if day.get("fare_type") == "split" else ""
        lines.append(
            f" {i}. {format_trip_dates(day['date'], day.get('return_date'))}{note} - "
            f"{format_price(day['price'], cur)}{suffix}  {_book_links(day, max_stops, cur)}"
        )

    lines += [
        "",
        f"📈 Avg: {format_price(result.avg_price, cur)} | Low: {format_price(result.min_price, cur)}"
        f" | High: {format_price(result.max_price, cur)}",
    ]
    if stats and stats.get("median") is not None:
        lines.append(
            f"📉 Hist median: {format_price(stats['median'], cur)} | Hist low: {format_price(stats['min'], cur)}"
        )
    trend = _trend_line(result.cheapest_price, prev_cheapest)
    if trend:
        lines += ["", trend]
    if result.partial:
        lines += ["", f"⚠️ Partial scan: {DEFER_REASONS.get(result.partial_reason, result.partial_reason)}"]
    lines.append(f"🔎 {result.probes_checked} dates checked · {result.requests_used} Google requests")
    return "\n".join(lines)


def format_alert_only_message(
    result: ScanResult, alert_lines: list[str], fx_amounts: dict[str, float] | None = None
) -> str:
    best = result.cheapest
    lines = [f"🚨 Deal alert: {_result_label(result)}", "", *alert_lines, ""]
    lines.append(f"Price: {format_price_multi(best['price'], result.currency, fx_amounts)}")
    lines.append(f"Dates: {format_trip_dates(best['date'], best.get('return_date'))}")
    if best.get("airline"):
        lines.append(f"Airline: {md(best['airline'])}")
    if best.get("fare_type") == "split":
        lines.append(_book_links(best, "any", result.currency))
    else:
        lines.append(f"[Book →]({_day_url(best, 'any', result.currency)})")
    return "\n".join(lines)


def format_history_message(
    route: dict, history: list[dict], *, stats: dict | None = None,
    fx_amounts: dict[str, float] | None = None, currency: str | None = None,
) -> str:
    code = currency or CURRENCY
    label = route_label(route)
    rows = list(reversed(history))
    prices = [h["cheapest_price"] for h in rows] or (stats or {}).get("prices", [])
    if not prices:
        return f"📉 {label} | No history yet"

    low, high = min(prices), max(prices)
    spread = (high - low) or 1
    lines = [f"📉 {label} | {len(rows) or len(prices)}-obs Price Trend", SEPARATOR, ""]
    for h in rows:
        price = h["cheapest_price"]
        bar = "█" * (int(((price - low) / spread) * 15) + 1)
        day = datetime.strptime(h["scan_date"], "%Y-%m-%d").strftime("%b %d")
        marker = "  ← lowest" if price == low else ""
        lines.append(f"{day}: {format_price(price, h.get('currency') or code)}  {bar}{marker}")
    lines.append("")
    if stats and stats.get("min") is not None:
        lines.append(f"Low: {format_price(stats['min'], code)} | Median: {format_price(stats['median'], code)}")
    if stats and stats.get("latest") is not None:
        lines.append(f"Latest: {format_price_multi(stats['latest'], code, fx_amounts)}")
    if len(prices) >= 2:
        pct = ((prices[-1] - prices[0]) / prices[0]) * 100
        lines.append(f"📉 Trend: {'Down' if pct < 0 else 'Up'} {abs(pct):.0f}% over period")
    if rows:
        latest = rows[-1]
        lines.append(
            f"💡 Best day found (latest scan): "
            f"{format_trip_dates(latest['cheapest_travel_date'], latest.get('cheapest_return_date'))} @ "
            f"{format_price(latest['cheapest_price'], latest.get('currency') or code)}"
        )
    return "\n".join(lines)


def _minutes(secs: float) -> int:
    return max(1, int((secs + 59) // 60))


def format_deferred_message(label: str, reason: str, retry_after_secs: float) -> str:
    why = DEFER_REASONS.get(reason, reason)
    minutes = _minutes(retry_after_secs)
    if reason.startswith("budget"):
        return (
            f"⏳ {label}\n"
            f"Scan postponed: {why}.\n"
            f"Resuming in ~{minutes} min. Raise REQ_BUDGET_* only if you use proxies."
        )
    return (
        f"⏳ {label}\n"
        f"Scan postponed: {why}.\n"
        f"All Google requests are paused for ~{minutes} min to protect this IP; "
        "the route is retried automatically.\n"
        "Avoid /check until then. Setting PROXY_URLS helps if this keeps happening."
    )


def format_error_message(label: str, *, retry_hours: float | None = None) -> str:
    when = f"Will retry in {retry_hours:g} hours." if retry_hours else "Will retry on the next scheduled scan."
    return f"⚠️ {label}\nScan failed (network or parsing). {when}\nRun /check to try manually."


def format_no_matches_message(label: str, stops_label: str) -> str:
    return (
        f"✈️ {label}\n"
        f"No flights found matching filter: {stops_label}\n"
        "Try a less restrictive stops preference via /stops or /routes."
    )


def format_guard_status(snapshot: dict) -> str:
    lines = [
        "🛡 Google request guard",
        f"Last hour: {snapshot['requests_hour']}/{snapshot['per_hour']}",
        f"Last 24h: {snapshot['requests_day']}/{snapshot['per_day']}",
    ]
    if snapshot.get("circuit_reason"):
        lines.append(
            f"Paused: {DEFER_REASONS.get(snapshot['circuit_reason'], snapshot['circuit_reason'])}"
            f" (~{_minutes(snapshot['circuit_remaining'])} min left)"
        )
    else:
        lines.append("Status: open")
    proxies = snapshot.get("proxies") or []
    if proxies:
        ok = sum(1 for _, wait in proxies if wait <= 0)
        lines.append(f"Proxies: {ok}/{len(proxies)} available")
    else:
        lines.append("Proxies: none (direct connection)")
    return "\n".join(lines)


def _nights_label(nights: int) -> str:
    if nights <= 0:
        return "mesmo dia"
    return "1 noite" if nights == 1 else f"{nights} noites"


def _via_leg_line(leg: dict, max_stops: str, currency: str) -> str:
    url = leg.get("booking_url") or booking_url(
        leg["from_airport"], leg["to_airport"], leg["date"], max_stops=max_stops, currency=currency
    )
    bits = [format_date(leg["date"]), f"{leg['from_airport']}→{leg['to_airport']}"]
    if leg.get("departure"):
        bits.append(leg["departure"])
    if leg.get("airline"):
        bits.append(md(str(leg["airline"])))
    bits.append(format_price(leg["price"], currency))
    return f"{' · '.join(bits)}  [Book →]({url})"


def format_via_message(
    result: ScanResult,
    *,
    max_stops: str = "any",
    fx_amounts: dict[str, float] | None = None,
    alert_lines: list[str] | None = None,
    prev_cheapest: float | None = None,
) -> str:
    from bot.search.hubs import city_label

    cur = result.currency
    stay_txt = f"{stay_label(result.stay_days, result.stay_days_max)} no destino"
    hub_txt = f"parada hub {stay_label(result.hub_nights or 0, result.hub_nights_max, ' noites')}"
    lines = [f"✈️ {result.from_airport} ⇄ {result.to_airport} via SP/RJ | {stay_txt} | {hub_txt}", SEPARATOR, ""]
    if alert_lines:
        lines += [*alert_lines, ""]

    lines.append(f"🏆 Melhor: {format_price_multi(result.cheapest_price, cur, fx_amounts)}")
    if result.direct_price is not None:
        delta = result.direct_price - result.cheapest_price
        if delta > 0:
            lines.append(
                f"   Direto: {format_price(result.direct_price, cur)} (−{delta / result.direct_price * 100:.0f}%)"
            )
        else:
            lines.append(f"   Direto: {format_price(result.direct_price, cur)} (via hub não ficou mais barato)")

    combos = result.via_combos or result.top_days
    best = combos[0]
    legs = best.get("legs") or []
    if len(legs) == 4:
        lines.append(f"   Ida via {city_label(best.get('hub_out') or '')} ({_nights_label(best.get('nights_out') or 0)}):")
        lines.append(f"   {_via_leg_line(legs[0], max_stops, cur)}")
        lines.append(f"   {_via_leg_line(legs[1], max_stops, cur)}")
        lines.append(f"   Volta via {city_label(best.get('hub_back') or '')} ({_nights_label(best.get('nights_back') or 0)}):")
        lines.append(f"   {_via_leg_line(legs[2], max_stops, cur)}")
        lines.append(f"   {_via_leg_line(legs[3], max_stops, cur)}")

    if len(combos) > 1:
        lines += ["", f"📊 Top {len(combos)}:"]
        for i, combo in enumerate(combos, 1):
            lines.append(
                f" {i}. {format_date(combo['date'])} → {format_date(combo['return_date'])}"
                f" - {format_price(combo['price'], cur)}"
            )
            route_bits = " + ".join(f"{l['from_airport']}→{l['to_airport']}" for l in combo.get("legs") or [])
            if route_bits:
                lines.append(f"    {route_bits}")

    lines += ["", "⚠️ Passagens separadas: atraso, bagagem e troca de aeroporto ficam por sua conta."]
    if prev_cheapest and prev_cheapest > 0:
        pct = ((result.cheapest_price - prev_cheapest) / prev_cheapest) * 100
        if pct < 0:
            lines.append(f"💡 Caiu {abs(pct):.0f}% desde o último scan")
        elif pct > 0:
            lines.append(f"💡 Subiu {pct:.0f}% desde o último scan")
    if result.partial:
        lines.append(f"⚠️ Scan parcial: {DEFER_REASONS.get(result.partial_reason, result.partial_reason)}")
    lines.append(f"🔎 {result.requests_used} Google requests")
    return "\n".join(lines)

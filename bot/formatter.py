import base64
from datetime import datetime

from bot.config import CURRENCY, CURRENCY_SYMBOL, DAYS_TO_SCAN, DISPLAY_CURRENCIES
from bot.scanner import ScanResult


def _format_price(price: float, currency: str | None = None) -> str:
    code = currency or CURRENCY
    symbol = CURRENCY_SYMBOL.get(code, code + " ")
    return f"{symbol}{price:,.0f}"


def _format_price_multi(
    price: float,
    currency: str | None = None,
    fx_amounts: dict[str, float] | None = None,
) -> str:
    primary = _format_price(price, currency)
    if not fx_amounts:
        return primary
    extras = []
    base = currency or CURRENCY
    for code in DISPLAY_CURRENCIES:
        if code == base:
            continue
        if code in fx_amounts:
            extras.append(_format_price(fx_amounts[code], code))
    if not extras:
        return primary
    return f"{primary} (≈ {' / '.join(extras)})"


def _format_date(date_str: str) -> str:
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return dt.strftime("%b %d (%a)")


def _format_trip_dates(outbound_date: str, return_date: str | None = None) -> str:
    if return_date:
        return f"{_format_date(outbound_date)} → {_format_date(return_date)}"
    return _format_date(outbound_date)


def _format_duration(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return f"{h}h {m}m"


def _format_stops(stops: int) -> str:
    if stops == 0:
        return "Nonstop"
    return f"{stops} stop{'s' if stops > 1 else ''}"


def _pb_varint(value: int) -> bytes:
    result = bytearray()
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)


def _pb_tag(field_number: int, wire_type: int) -> bytes:
    return _pb_varint((field_number << 3) | wire_type)


def _pb_string(field_number: int, value: str) -> bytes:
    encoded = value.encode("utf-8")
    return _pb_tag(field_number, 2) + _pb_varint(len(encoded)) + encoded


def _pb_message(field_number: int, data: bytes) -> bytes:
    return _pb_tag(field_number, 2) + _pb_varint(len(data)) + data


def _pb_enum(field_number: int, value: int) -> bytes:
    return _pb_tag(field_number, 0) + _pb_varint(value)


_URL_STOPS_MAP = {
    "direct": 1,
    "1stop": 2,
    "2stops": 3,
}


def _flight_data(from_airport: str, to_airport: str, date: str, max_stops: str = "any") -> bytes:
    from_ap = _pb_string(2, from_airport)
    to_ap = _pb_string(2, to_airport)

    flight_data = _pb_string(2, date)
    stops_val = _URL_STOPS_MAP.get(max_stops)
    if stops_val is not None:
        flight_data += _pb_enum(5, stops_val)
    flight_data += _pb_message(13, from_ap)
    flight_data += _pb_message(14, to_ap)
    return flight_data


def _flight_url(
    from_airport: str,
    to_airport: str,
    outbound_date: str,
    max_stops: str = "any",
    return_date: str | None = None,
) -> str:
    """Build a Google Flights search URL for a one-way or round-trip flight."""
    # Use first airport if comma-separated list is passed
    origin = from_airport.split(",")[0].strip()
    dest = to_airport.split(",")[0].strip()
    outbound = _flight_data(origin, dest, outbound_date, max_stops=max_stops)

    if return_date:
        inbound = _flight_data(dest, origin, return_date, max_stops=max_stops)
        info = _pb_message(3, outbound) + _pb_message(3, inbound)
        info += _pb_enum(8, 1)
        info += _pb_enum(9, 1)
        info += _pb_enum(19, 1)
    else:
        info = _pb_message(3, outbound)
        info += _pb_enum(8, 1)
        info += _pb_enum(9, 1)
        info += _pb_enum(19, 2)

    tfs = base64.urlsafe_b64encode(info).decode("ascii").rstrip("=")
    return f"https://www.google.com/travel/flights/search?tfs={tfs}"


def _natural_flight_url(
    from_airport: str,
    to_airport: str,
    outbound_date: str,
    return_date: str | None = None,
    one_way: bool = False,
) -> str:
    """Fallback natural-language Google Flights URL (skill-compatible)."""
    origin = from_airport.split(",")[0].strip()
    dest = to_airport.split(",")[0].strip()
    q = f"Flights+from+{origin}+to+{dest}+on+{outbound_date}"
    if return_date and not one_way:
        q += f"+returning+{return_date}"
    elif one_way or not return_date:
        q += "+one+way"
    return f"https://www.google.com/travel/flights?q={q}"


def _book_links_for_day(day: dict, result: ScanResult, max_stops: str) -> str:
    origin = day.get("from_airport") or result.cheapest_from or result.from_airport
    dest = day.get("to_airport") or result.cheapest_to or result.to_airport
    fare_type = day.get("fare_type") or result.fare_type

    if fare_type == "split" and day.get("return_date"):
        out_url = _flight_url(origin, dest, day["date"], max_stops=max_stops)
        in_url = _flight_url(dest, origin, day["return_date"], max_stops=max_stops)
        return f"[Out →]({out_url}) · [Return →]({in_url})"

    url = _flight_url(
        origin,
        dest,
        day["date"],
        max_stops=max_stops,
        return_date=day.get("return_date") if fare_type != "oneway" else None,
    )
    return f"[Book →]({url})"


def format_daily_message(
    result: ScanResult,
    prev_cheapest: float | None = None,
    stops_label: str | None = None,
    max_stops: str = "any",
    fx_amounts: dict[str, float] | None = None,
    alert_lines: list[str] | None = None,
    stats: dict | None = None,
) -> str:
    is_roundtrip = result.stay_days is not None
    route_label = (
        f"{result.from_airport} ⇄ {result.to_airport}"
        if is_roundtrip
        else f"{result.from_airport} → {result.to_airport}"
    )
    header = f"✈️ {route_label} | Next {DAYS_TO_SCAN} Days"
    if is_roundtrip:
        if result.stay_days_max and result.stay_days_max != result.stay_days:
            header += f" | {result.stay_days}-{result.stay_days_max}-day stay"
        else:
            header += f" | {result.stay_days}-day stay"
    if stops_label:
        header += f" | Filter: {stops_label}"
    lines = [
        header,
        "━━━━━━━━━━━━━━━━━━━━━━",
        "",
    ]

    if alert_lines:
        lines.extend(alert_lines)
        lines.append("")

    cheapest_dates = _format_trip_dates(result.cheapest_travel_date, result.cheapest_return_date)
    cheapest_line = (
        f"🏆 Cheapest: {cheapest_dates} - "
        f"{_format_price_multi(result.cheapest_price, result.currency, fx_amounts)}"
    )
    lines.append(cheapest_line)

    if result.cheapest_from and result.cheapest_to:
        if result.cheapest_from != result.from_airport.split(",")[0] or result.cheapest_to != result.to_airport.split(",")[0]:
            lines.append(f"   Airports: {result.cheapest_from} → {result.cheapest_to}")

    if result.fare_type == "split":
        out_p = result.outbound_price
        in_p = result.inbound_price
        lines.append(
            f"   ⚠️ Separate tickets"
            + (f" (out {_format_price(out_p, result.currency)} + return {_format_price(in_p, result.currency)})" if out_p and in_p else "")
        )

    if result.cheapest_airline:
        detail_parts = [result.cheapest_airline]
        if result.cheapest_departure:
            detail_parts.append(result.cheapest_departure)
        if result.cheapest_duration is not None:
            detail_parts.append(_format_duration(result.cheapest_duration))
        if result.cheapest_stops is not None:
            detail_parts.append(_format_stops(result.cheapest_stops))
        lines.append(f"   {' | '.join(detail_parts)}")

    lines.append("")
    lines.append(f"📊 Top {len(result.top_days)} Cheapest Days:")
    for i, day in enumerate(result.top_days, 1):
        links = _book_links_for_day(day, result, max_stops)
        day_label = _format_trip_dates(day["date"], day.get("return_date"))
        price_txt = _format_price(day["price"], result.currency)
        suffix = ""
        if day.get("fare_type") == "split":
            suffix = " · split tickets"
        airport_note = ""
        if day.get("from_airport") and day.get("to_airport"):
            if day["from_airport"] != result.from_airports[0] or day["to_airport"] != result.to_airports[0]:
                airport_note = f" ({day['from_airport']}→{day['to_airport']})"
        lines.append(f" {i}. {day_label}{airport_note} - {price_txt}{suffix}  {links}")

    lines.append("")
    lines.append(
        f"📈 Avg: {_format_price(result.avg_price, result.currency)} | "
        f"Low: {_format_price(result.min_price, result.currency)} | "
        f"High: {_format_price(result.max_price, result.currency)}"
    )

    if stats and stats.get("median") is not None:
        lines.append(
            f"📉 Hist median: {_format_price(stats['median'], result.currency)} | "
            f"Hist low: {_format_price(stats['min'], result.currency)}"
        )

    if prev_cheapest is not None and prev_cheapest > 0:
        pct = ((result.cheapest_price - prev_cheapest) / prev_cheapest) * 100
        if pct < 0:
            lines.append(f"\n💡 Trend: Prices dropped {abs(pct):.0f}% since last scan")
        elif pct > 0:
            lines.append(f"\n💡 Trend: Prices rose {pct:.0f}% since last scan")
        else:
            lines.append("\n💡 Trend: Prices unchanged since last scan")

    return "\n".join(lines)


def format_history_message(
    from_airport: str,
    to_airport: str,
    history: list[dict],
    stay_days: int | None = None,
    stay_days_max: int | None = None,
    stats: dict | None = None,
    fx_amounts: dict[str, float] | None = None,
    currency: str | None = None,
) -> str:
    code = currency or CURRENCY
    is_rt = stay_days is not None
    route_label = f"{from_airport} ⇄ {to_airport}" if is_rt else f"{from_airport} → {to_airport}"
    if not history and not (stats and stats.get("count")):
        return f"📉 {route_label} | No history yet"

    history = list(reversed(history)) if history else []
    prices = [h["cheapest_price"] for h in history] if history else (stats or {}).get("prices", [])
    if not prices:
        return f"📉 {route_label} | No history yet"

    min_price = min(prices)
    max_price = max(prices)
    price_range = max_price - min_price if max_price != min_price else 1

    stay_note = ""
    if stay_days is not None:
        if stay_days_max and stay_days_max != stay_days:
            stay_note = f" | {stay_days}-{stay_days_max}d"
        else:
            stay_note = f" | {stay_days}d"

    lines = [
        f"📉 {route_label}{stay_note} | {len(history) or stats.get('count', 0)}-obs Price Trend",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
    ]

    max_bar_len = 15
    for h in history:
        price = h["cheapest_price"]
        bar_len = int(((price - min_price) / price_range) * max_bar_len) + 1
        bar = "█" * bar_len
        date_str = datetime.strptime(h["scan_date"], "%Y-%m-%d").strftime("%b %d")
        marker = "  ← lowest" if price == min_price else ""
        cur = h.get("currency") or code
        lines.append(f"{date_str}: {_format_price(price, cur)}  {bar}{marker}")

    lines.append("")

    if stats:
        if stats.get("min") is not None:
            lines.append(f"Low: {_format_price(stats['min'], code)} | Median: {_format_price(stats['median'], code)}")
        if stats.get("latest") is not None:
            latest_txt = _format_price_multi(stats["latest"], code, fx_amounts)
            lines.append(f"Latest: {latest_txt}")

    if len(prices) >= 2:
        pct = ((prices[-1] - prices[0]) / prices[0]) * 100
        direction = "Down" if pct < 0 else "Up"
        lines.append(f"📉 Trend: {direction} {abs(pct):.0f}% over period")

    if history:
        latest = history[-1]
        best_dates = _format_trip_dates(
            latest.get("cheapest_travel_date", latest["scan_date"]),
            latest.get("cheapest_return_date"),
        )
        lines.append(
            f"💡 Best day found (latest scan): {best_dates} @ "
            f"{_format_price(latest['cheapest_price'], latest.get('currency') or code)}"
        )

    return "\n".join(lines)


def format_error_message(
    from_airport: str,
    to_airport: str,
    stay_days: int | None = None,
) -> str:
    route_label = (
        f"{from_airport} ⇄ {to_airport} ({stay_days} days)"
        if stay_days
        else f"{from_airport} → {to_airport}"
    )
    return (
        f"⚠️ {route_label}\n"
        "Scan failed. Will retry in 4 hours.\n"
        "If this keeps happening, the flight data library may need updating."
    )


def format_retry_failed_message(
    from_airport: str,
    to_airport: str,
    stay_days: int | None = None,
) -> str:
    route_label = (
        f"{from_airport} ⇄ {to_airport} ({stay_days} days)"
        if stay_days
        else f"{from_airport} → {to_airport}"
    )
    return (
        f"❌ {route_label}\n"
        "Scan failed after retry. Will try again on next scheduled scan.\n"
        "Run /check to try manually."
    )


def format_alert_only_message(
    result: ScanResult,
    alert_lines: list[str],
    fx_amounts: dict[str, float] | None = None,
) -> str:
    route_label = (
        f"{result.from_airport} ⇄ {result.to_airport}"
        if result.stay_days is not None
        else f"{result.from_airport} → {result.to_airport}"
    )
    lines = [f"🚨 Deal alert: {route_label}", ""]
    lines.extend(alert_lines)
    lines.append("")
    lines.append(
        f"Price: {_format_price_multi(result.cheapest_price, result.currency, fx_amounts)}"
    )
    lines.append(
        f"Dates: {_format_trip_dates(result.cheapest_travel_date, result.cheapest_return_date)}"
    )
    return "\n".join(lines)

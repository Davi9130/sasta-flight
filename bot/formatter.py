import base64
from datetime import datetime
from bot.config import CURRENCY, CURRENCY_SYMBOL
from bot.scanner import ScanResult


def _format_price(price: float) -> str:
    symbol = CURRENCY_SYMBOL[CURRENCY]
    return f"{symbol}{price:,.0f}"


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


# Google Flights protobuf field encoding helpers
def _pb_varint(value: int) -> bytes:
    """Encode an integer as a protobuf varint."""
    result = bytearray()
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)


def _pb_tag(field_number: int, wire_type: int) -> bytes:
    """Encode a protobuf field tag."""
    return _pb_varint((field_number << 3) | wire_type)


def _pb_string(field_number: int, value: str) -> bytes:
    """Encode a string field."""
    encoded = value.encode("utf-8")
    return _pb_tag(field_number, 2) + _pb_varint(len(encoded)) + encoded


def _pb_message(field_number: int, data: bytes) -> bytes:
    """Encode a nested message field."""
    return _pb_tag(field_number, 2) + _pb_varint(len(data)) + data


def _pb_enum(field_number: int, value: int) -> bytes:
    """Encode an enum/int32 field."""
    return _pb_tag(field_number, 0) + _pb_varint(value)


# Mapping from bot stops preference to Google Flights protobuf max_stops values
_URL_STOPS_MAP = {
    "direct": 1,   # Nonstop only
    "1stop": 2,    # 1 stop or fewer
    "2stops": 3,   # 2 stops or fewer
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
    outbound = _flight_data(from_airport, to_airport, outbound_date, max_stops=max_stops)

    if return_date:
        inbound = _flight_data(to_airport, from_airport, return_date, max_stops=max_stops)
        info = _pb_message(3, outbound) + _pb_message(3, inbound)
        info += _pb_enum(8, 1)   # ADULT
        info += _pb_enum(9, 1)   # ECONOMY
        info += _pb_enum(19, 1)  # ROUND_TRIP
    else:
        info = _pb_message(3, outbound)
        info += _pb_enum(8, 1)   # ADULT
        info += _pb_enum(9, 1)   # ECONOMY
        info += _pb_enum(19, 2)  # ONE_WAY

    tfs = base64.urlsafe_b64encode(info).decode("ascii").rstrip("=")
    return f"https://www.google.com/travel/flights/search?tfs={tfs}"


def format_daily_message(result: ScanResult, prev_cheapest: float | None = None, stops_label: str | None = None, max_stops: str = "any") -> str:
    is_roundtrip = result.stay_days is not None
    route_label = f"{result.from_airport} ⇄ {result.to_airport}" if is_roundtrip else f"{result.from_airport} → {result.to_airport}"
    header = f"✈️ {route_label} | Next 30 Days"
    if is_roundtrip:
        header += f" | {result.stay_days}-day stay"
    if stops_label:
        header += f" | Filter: {stops_label}"
    lines = [
        header,
        "━━━━━━━━━━━━━━━━━━━━━━",
        "",
    ]

    cheapest_dates = _format_trip_dates(result.cheapest_travel_date, result.cheapest_return_date)
    cheapest_line = f"🏆 Cheapest: {cheapest_dates} - {_format_price(result.cheapest_price)}"
    lines.append(cheapest_line)

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
        url = _flight_url(
            result.from_airport,
            result.to_airport,
            day["date"],
            max_stops=max_stops,
            return_date=day.get("return_date"),
        )
        day_label = _format_trip_dates(day["date"], day.get("return_date"))
        lines.append(f" {i}. {day_label} - {_format_price(day['price'])}  [Book →]({url})")

    lines.append("")

    lines.append(
        f"📈 Avg: {_format_price(result.avg_price)} | "
        f"Low: {_format_price(result.min_price)} | "
        f"High: {_format_price(result.max_price)}"
    )

    if prev_cheapest is not None:
        pct = ((result.cheapest_price - prev_cheapest) / prev_cheapest) * 100
        if pct < 0:
            lines.append(f"\n💡 Trend: Prices dropped {abs(pct):.0f}% since yesterday")
        elif pct > 0:
            lines.append(f"\n💡 Trend: Prices rose {pct:.0f}% since yesterday")
        else:
            lines.append("\n💡 Trend: Prices unchanged since yesterday")

    return "\n".join(lines)


def format_history_message(
    from_airport: str,
    to_airport: str,
    history: list[dict],
    stay_days: int | None = None,
) -> str:
    if not history:
        route_label = f"{from_airport} ⇄ {to_airport}" if stay_days else f"{from_airport} → {to_airport}"
        return f"📉 {route_label} | No history yet"

    history = list(reversed(history))

    prices = [h["cheapest_price"] for h in history]
    min_price = min(prices)
    max_price = max(prices)
    price_range = max_price - min_price if max_price != min_price else 1

    route_label = f"{from_airport} ⇄ {to_airport}" if stay_days else f"{from_airport} → {to_airport}"
    lines = [
        f"📉 {route_label} | {len(history)}-Day Price Trend",
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
        lines.append(f"{date_str}: {_format_price(price)}  {bar}{marker}")

    lines.append("")

    if len(prices) >= 2:
        pct = ((prices[-1] - prices[0]) / prices[0]) * 100
        direction = "Down" if pct < 0 else "Up"
        lines.append(f"📉 Trend: {direction} {abs(pct):.0f}% this week")

    latest = history[-1]
    best_dates = _format_trip_dates(
        latest.get("cheapest_travel_date", latest["scan_date"]),
        latest.get("cheapest_return_date"),
    )
    lines.append(
        f"💡 Best day to fly found today: {best_dates} @ {_format_price(latest['cheapest_price'])}"
    )

    return "\n".join(lines)


def format_error_message(from_airport: str, to_airport: str) -> str:
    return (
        f"⚠️ {from_airport} → {to_airport}\n"
        "Scan failed. Will retry in 4 hours.\n"
        "If this keeps happening, the flight data library may need updating."
    )


def format_retry_failed_message(from_airport: str, to_airport: str) -> str:
    return (
        f"❌ {from_airport} → {to_airport}\n"
        "Scan failed after retry. Will try again on next scheduled scan.\n"
        "Run /check to try manually."
    )

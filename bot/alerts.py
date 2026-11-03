"""Alert rules. Silent by default: only rules configured on the route fire."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from bot.config import CURRENCY, DEFAULT_ALERT_COOLDOWN_MINUTES


@dataclass
class AlertHit:
    rule: str
    message: str
    fingerprint: str
    price: float


def _fingerprint(route_id: int, rule: str, price: float, travel_date: str) -> str:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"{route_id}:{rule}:{travel_date}:{int(round(price))}:{day}"


async def evaluate_alerts(
    db,
    route: dict,
    *,
    price: float,
    travel_date: str,
    prev_price: float | None,
    stats: dict | None,
    currency: str = CURRENCY,
) -> list[AlertHit]:
    """Hits to send; the caller dedupes through `record_alert_event`."""
    route_id = route["id"]
    cooldown = route.get("alert_cooldown_minutes") or DEFAULT_ALERT_COOLDOWN_MINUTES
    if await db.is_alert_cooling_down(route_id, int(cooldown)):
        return []

    hits: list[AlertHit] = []
    target = route.get("target_price")
    if target is not None and price <= float(target):
        hits.append(
            AlertHit(
                rule="target",
                message=f"🎯 Target hit: price ≤ {float(target):g} {currency}",
                fingerprint=_fingerprint(route_id, "target", price, travel_date),
                price=price,
            )
        )

    drop_pct = route.get("alert_drop_pct")
    if drop_pct is not None and prev_price and prev_price > 0:
        pct = ((price - prev_price) / prev_price) * 100
        if pct <= -float(drop_pct):
            hits.append(
                AlertHit(
                    rule="drop",
                    message=f"📉 Dropped {abs(pct):.0f}% vs last scan (threshold {float(drop_pct):g}%)",
                    fingerprint=_fingerprint(route_id, "drop", price, travel_date),
                    price=price,
                )
            )

    if route.get("alert_on_new_low") == 1:
        hist_min = (stats or {}).get("min")
        if hist_min is not None and price < float(hist_min):
            hits.append(
                AlertHit(
                    rule="new_low",
                    message=f"🏆 New historical low (was {float(hist_min):g} {currency})",
                    fingerprint=_fingerprint(route_id, "new_low", price, travel_date),
                    price=price,
                )
            )
    return hits

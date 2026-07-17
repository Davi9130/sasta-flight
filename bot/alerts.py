"""Alert evaluation for price opportunities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from bot.config import (
    CURRENCY,
    DEFAULT_ALERT_COOLDOWN_MINUTES,
    DEFAULT_ALERT_DROP_PCT,
)


@dataclass
class AlertHit:
    rule: str
    message: str
    fingerprint: str
    price: float


def _fingerprint(route_id: int, rule: str, price: float, travel_date: str) -> str:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Bucket price to avoid spam on tiny fluctuations within same day/rule
    bucket = int(round(price))
    return f"{route_id}:{rule}:{travel_date}:{bucket}:{day}"


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
    """Return alert hits that should be sent (dedupe/cooldown applied by caller via db)."""
    hits: list[AlertHit] = []
    route_id = route["id"]
    cooldown = route.get("alert_cooldown_minutes") or DEFAULT_ALERT_COOLDOWN_MINUTES
    drop_pct = route.get("alert_drop_pct")
    if drop_pct is None:
        drop_pct = DEFAULT_ALERT_DROP_PCT
    alert_on_new_low = route.get("alert_on_new_low")
    if alert_on_new_low is None:
        alert_on_new_low = 1

    cooling = await db.is_alert_cooling_down(route_id, int(cooldown))
    if cooling:
        return []

    target = route.get("target_price")
    if target is not None and price <= float(target):
        hits.append(
            AlertHit(
                rule="target",
                message=f"🎯 Target hit: price ≤ {target:g} {currency}",
                fingerprint=_fingerprint(route_id, "target", price, travel_date),
                price=price,
            )
        )

    if prev_price and prev_price > 0 and drop_pct is not None:
        pct = ((price - prev_price) / prev_price) * 100
        if pct <= -float(drop_pct):
            hits.append(
                AlertHit(
                    rule="drop",
                    message=f"📉 Dropped {abs(pct):.0f}% vs last scan (threshold {drop_pct:g}%)",
                    fingerprint=_fingerprint(route_id, "drop", price, travel_date),
                    price=price,
                )
            )

    hist_min = (stats or {}).get("min")
    if alert_on_new_low and hist_min is not None and price < float(hist_min):
        hits.append(
            AlertHit(
                rule="new_low",
                message=f"🏆 New historical low (was {hist_min:g} {currency})",
                fingerprint=_fingerprint(route_id, "new_low", price, travel_date),
                price=price,
            )
        )

    return hits

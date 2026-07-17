import pytest
from unittest.mock import AsyncMock, MagicMock

from bot.alerts import evaluate_alerts


@pytest.mark.asyncio
async def test_alert_target_price():
    db = MagicMock()
    db.is_alert_cooling_down = AsyncMock(return_value=False)
    route = {"id": 1, "target_price": 3000, "alert_drop_pct": 10, "alert_on_new_low": 0}
    hits = await evaluate_alerts(
        db, route, price=2900, travel_date="2026-03-18", prev_price=3500, stats={"min": 2800}
    )
    assert any(h.rule == "target" for h in hits)


@pytest.mark.asyncio
async def test_alert_drop_pct():
    db = MagicMock()
    db.is_alert_cooling_down = AsyncMock(return_value=False)
    route = {"id": 1, "target_price": None, "alert_drop_pct": 5, "alert_on_new_low": 0}
    hits = await evaluate_alerts(
        db, route, price=2800, travel_date="2026-03-18", prev_price=3500, stats=None
    )
    assert any(h.rule == "drop" for h in hits)


@pytest.mark.asyncio
async def test_alert_new_low():
    db = MagicMock()
    db.is_alert_cooling_down = AsyncMock(return_value=False)
    route = {"id": 1, "target_price": None, "alert_drop_pct": 50, "alert_on_new_low": 1}
    hits = await evaluate_alerts(
        db, route, price=2500, travel_date="2026-03-18", prev_price=2600, stats={"min": 2550}
    )
    assert any(h.rule == "new_low" for h in hits)


@pytest.mark.asyncio
async def test_alert_cooldown_blocks():
    db = MagicMock()
    db.is_alert_cooling_down = AsyncMock(return_value=True)
    route = {"id": 1, "target_price": 3000, "alert_drop_pct": 1, "alert_on_new_low": 1}
    hits = await evaluate_alerts(
        db, route, price=1000, travel_date="2026-03-18", prev_price=5000, stats={"min": 4000}
    )
    assert hits == []

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from bot.fx import FxService


@pytest.mark.asyncio
async def test_fx_uses_cache_when_fresh():
    db = MagicMock()
    db.get_fx_rate = AsyncMock(
        return_value={
            "base": "BRL",
            "quote": "USD",
            "rate": 0.18,
            "fetched_at": "2099-01-01T00:00:00+00:00",
        }
    )
    db.save_fx_rate = AsyncMock()
    fx = FxService(db)

    with patch.object(fx, "_fetch_rates", new_callable=AsyncMock) as fetch:
        rates = await fx.get_rates("BRL", quotes=("USD",))
        fetch.assert_not_called()
    assert rates["USD"] == 0.18


@pytest.mark.asyncio
async def test_fx_fetches_and_stores_when_missing():
    db = MagicMock()
    db.get_fx_rate = AsyncMock(return_value=None)
    db.save_fx_rate = AsyncMock()
    fx = FxService(db)

    with patch.object(fx, "_fetch_rates", new_callable=AsyncMock, return_value={"EUR": 0.16}):
        rates = await fx.get_rates("BRL", quotes=("EUR",))
    assert rates["EUR"] == 0.16
    db.save_fx_rate.assert_called()


@pytest.mark.asyncio
async def test_fx_falls_back_to_stale_cache():
    db = MagicMock()
    db.get_fx_rate = AsyncMock(
        side_effect=[
            None,  # fresh miss
            {"base": "BRL", "quote": "USD", "rate": 0.17, "fetched_at": "2000-01-01T00:00:00+00:00"},
        ]
    )
    db.save_fx_rate = AsyncMock()
    fx = FxService(db)

    with patch.object(fx, "_fetch_rates", new_callable=AsyncMock, return_value={}):
        # First call in loop: fresh miss; after fetch empty, stale path calls again
        db.get_fx_rate = AsyncMock(
            return_value={
                "base": "BRL",
                "quote": "USD",
                "rate": 0.17,
                "fetched_at": "2000-01-01T00:00:00+00:00",
            }
        )
        # Force fresh miss then stale hit by controlling allow_stale via side effects
        async def get_fx(base, quote):
            return {
                "base": base,
                "quote": quote,
                "rate": 0.17,
                "fetched_at": "2000-01-01T00:00:00+00:00",
            }

        db.get_fx_rate = get_fx
        # Override _get_cached behavior indirectly: old timestamp makes fresh miss
        rates = await fx.get_rates("BRL", quotes=("USD",))
    assert rates["USD"] == 0.17


@pytest.mark.asyncio
async def test_fx_convert():
    fx = FxService(db=None)
    with patch.object(fx, "get_rates", new_callable=AsyncMock, return_value={"USD": 0.2, "EUR": 0.18}):
        amounts = await fx.convert(1000, base="BRL")
    assert amounts["USD"] == 200
    assert amounts["EUR"] == 180

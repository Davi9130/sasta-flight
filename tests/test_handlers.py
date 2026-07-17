import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from bot.scanner import ScanResult


@pytest.mark.asyncio
async def test_scheduled_scan_route_skips_when_paused():
    from bot.handlers import _scheduled_scan_route

    mock_context = MagicMock()
    mock_context.bot_data = {}
    mock_context.job.data = {"id": 1, "from_airport": "ATQ", "to_airport": "BOM"}

    with patch("bot.handlers.db") as mock_db, patch("bot.handlers._scan_and_send") as mock_scan:
        mock_db.get_config = AsyncMock(return_value="1")
        await _scheduled_scan_route(mock_context)
        mock_scan.assert_not_called()


@pytest.mark.asyncio
async def test_scheduled_scan_route_scans_when_not_paused():
    from bot.handlers import _scheduled_scan_route

    route = {"id": 1, "from_airport": "ATQ", "to_airport": "BOM", "is_active": 1}
    mock_context = MagicMock()
    mock_context.bot_data = {}
    mock_context.job.data = route

    with patch("bot.handlers.db") as mock_db, patch(
        "bot.handlers._scan_and_send", new_callable=AsyncMock
    ) as mock_scan:
        mock_db.get_config = AsyncMock(return_value="0")
        mock_db.get_route = AsyncMock(return_value=route)
        await _scheduled_scan_route(mock_context)
        mock_scan.assert_called_once_with(mock_context, route, use_lock=True)


@pytest.mark.asyncio
async def test_scan_and_send_skips_concurrent_scan():
    from bot.handlers import _scan_and_send

    route = {"id": 1, "from_airport": "ATQ", "to_airport": "BOM"}
    mock_context = MagicMock()
    mock_context.bot_data = {"_scanning_routes": {1}}
    mock_context.bot.send_message = AsyncMock()

    with patch("bot.handlers.scan_route", new_callable=AsyncMock) as mock_scan:
        await _scan_and_send(mock_context, route, use_lock=True)
        mock_scan.assert_not_called()


@pytest.mark.asyncio
async def test_scheduled_scan_route_refreshes_route():
    from bot.handlers import _scheduled_scan_route

    route = {"id": 1, "from_airport": "ATQ", "to_airport": "BOM", "is_active": 1}
    mock_context = MagicMock()
    mock_context.bot_data = {}
    mock_context.job.data = {"id": 1}

    with patch("bot.handlers.db") as mock_db, patch(
        "bot.handlers._scan_and_send", new_callable=AsyncMock
    ) as mock_scan:
        mock_db.get_config = AsyncMock(return_value="0")
        mock_db.get_route = AsyncMock(return_value=route)
        await _scheduled_scan_route(mock_context)
        mock_scan.assert_called_once_with(mock_context, route, use_lock=True)

@pytest.mark.asyncio
async def test_conditional_retry_skipped_for_short_interval():
    from bot.handlers import _scan_and_send

    route = {"id": 1, "from_airport": "ATQ", "to_airport": "BOM"}
    mock_context = MagicMock()
    mock_context.bot.send_message = AsyncMock()
    mock_context.job_queue.run_once = MagicMock()
    mock_context.bot_data = {}

    with patch("bot.handlers.db") as mock_db, patch(
        "bot.handlers.scan_route", new_callable=AsyncMock, return_value=None
    ):
        mock_db.get_route_stops_preference = AsyncMock(return_value="any")
        mock_db.get_route_scan_interval = AsyncMock(return_value=120)
        mock_db.get_previous_cheapest = AsyncMock(return_value=None)
        mock_db.get_route_price_stats = AsyncMock(return_value={"count": 0})
        mock_db.create_scan_run = AsyncMock(return_value=1)
        await _scan_and_send(mock_context, route)
        mock_context.job_queue.run_once.assert_not_called()


@pytest.mark.asyncio
async def test_conditional_retry_scheduled_for_long_interval():
    from bot.handlers import _scan_and_send

    route = {"id": 1, "from_airport": "ATQ", "to_airport": "BOM"}
    mock_context = MagicMock()
    mock_context.bot.send_message = AsyncMock()
    mock_context.job_queue.run_once = MagicMock()
    mock_context.job_queue.get_jobs_by_name = MagicMock(return_value=[])
    mock_context.bot_data = {}

    with patch("bot.handlers.db") as mock_db, patch(
        "bot.handlers.scan_route", new_callable=AsyncMock, return_value=None
    ):
        mock_db.get_route_stops_preference = AsyncMock(return_value="any")
        mock_db.get_route_scan_interval = AsyncMock(return_value=720)
        mock_db.get_previous_cheapest = AsyncMock(return_value=None)
        mock_db.get_route_price_stats = AsyncMock(return_value={"count": 0})
        mock_db.create_scan_run = AsyncMock(return_value=1)
        await _scan_and_send(mock_context, route)
        mock_context.job_queue.run_once.assert_called_once()


@pytest.mark.asyncio
async def test_retry_skipped_when_paused_or_removed():
    from bot.handlers import _retry_scan_job

    mock_context = MagicMock()
    mock_context.job.data = {"route_id": 1}
    mock_context.bot_data = {}

    with patch("bot.handlers.db") as mock_db, patch(
        "bot.handlers._scan_and_send", new_callable=AsyncMock
    ) as mock_scan:
        mock_db.get_config = AsyncMock(return_value="1")
        await _retry_scan_job(mock_context)
        mock_scan.assert_not_called()

        mock_db.get_config = AsyncMock(return_value="0")
        mock_db.get_route = AsyncMock(return_value=None)
        await _retry_scan_job(mock_context)
        mock_scan.assert_not_called()


@pytest.mark.asyncio
async def test_scan_and_send_rate_limited_message():
    from bot.handlers import _scan_and_send
    from bot.scanner import RATE_LIMITED

    route = {"id": 1, "from_airport": "ATQ", "to_airport": "BOM"}
    mock_context = MagicMock()
    mock_context.bot.send_message = AsyncMock()
    mock_context.job_queue.get_jobs_by_name = MagicMock(return_value=[])
    mock_context.job_queue.run_once = MagicMock()
    mock_context.bot_data = {}

    with patch("bot.handlers.db") as mock_db, patch(
        "bot.handlers.scan_route", new_callable=AsyncMock, return_value=RATE_LIMITED
    ), patch("bot.handlers.circuit_retry_after_secs", return_value=120):
        mock_db.get_route_stops_preference = AsyncMock(return_value="any")
        mock_db.get_previous_cheapest = AsyncMock(return_value=None)
        mock_db.get_route_price_stats = AsyncMock(return_value={"count": 0})
        mock_db.create_scan_run = AsyncMock(return_value=1)
        await _scan_and_send(mock_context, route)
        mock_db.create_scan_run.assert_called_once()
        assert mock_db.create_scan_run.call_args[0][1] == "rate_limited"
        sent = mock_context.bot.send_message.call_args
        text = sent.kwargs.get("text") or (sent.args[1] if len(sent.args) > 1 else "")
        assert "rate-limited" in text.lower() or "429" in text
        mock_context.job_queue.run_once.assert_called_once()


@pytest.mark.asyncio
async def test_scan_and_send_persists_snapshots_and_alerts():
    from bot.handlers import _scan_and_send

    route = {
        "id": 1,
        "from_airport": "ATQ",
        "to_airport": "BOM",
        "target_price": 3500,
        "alert_drop_pct": 5,
        "alert_on_new_low": 1,
        "alert_cooldown_minutes": 60,
    }
    result = ScanResult(
        from_airport="ATQ",
        to_airport="BOM",
        cheapest_price=3000,
        cheapest_travel_date="2026-03-18",
        cheapest_airline="IndiGo",
        cheapest_departure="06:00 AM",
        cheapest_duration=165,
        cheapest_stops=0,
        top_days=[{"date": "2026-03-18", "price": 3000, "from_airport": "ATQ", "to_airport": "BOM"}],
        avg_price=4000,
        min_price=3000,
        max_price=5000,
        currency="BRL",
        provider="fli",
        from_airports=["ATQ"],
        to_airports=["BOM"],
    )
    mock_context = MagicMock()
    mock_context.bot.send_message = AsyncMock()
    mock_context.bot_data = {}

    with patch("bot.handlers.db") as mock_db, patch(
        "bot.handlers.scan_route", new_callable=AsyncMock, return_value=result
    ), patch("bot.handlers.fx_service", None), patch(
        "bot.handlers.evaluate_alerts", new_callable=AsyncMock, return_value=[]
    ):
        mock_db.get_route_stops_preference = AsyncMock(return_value="any")
        mock_db.get_previous_cheapest = AsyncMock(return_value=4000)
        mock_db.get_route_price_stats = AsyncMock(return_value={"count": 1, "min": 3500, "median": 3600})
        mock_db.create_scan_run = AsyncMock(return_value=9)
        mock_db.save_fare_snapshots = AsyncMock()
        mock_db.save_price_history = AsyncMock()
        mock_db.get_config = AsyncMock(return_value="1")
        await _scan_and_send(mock_context, route)
        mock_db.save_fare_snapshots.assert_called_once()
        mock_db.save_price_history.assert_called_once()
        mock_context.bot.send_message.assert_called_once()

import pytest
import os
import json
from bot.db import Database

TEST_DB = "data/test_flights.db"


@pytest.fixture
async def db():
    os.makedirs("data", exist_ok=True)
    database = Database(TEST_DB)
    await database.init()
    yield database
    await database.close()
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)


@pytest.mark.asyncio
async def test_init_creates_tables(db):
    config = await db.get_config("notify_time")
    assert config == "08:00"
    paused = await db.get_config("is_paused")
    assert paused == "0"


@pytest.mark.asyncio
async def test_set_and_get_config(db):
    await db.set_config("notify_time", "10:30")
    assert await db.get_config("notify_time") == "10:30"


@pytest.mark.asyncio
async def test_add_and_get_routes(db):
    route_id = await db.add_route("ATQ", "BOM")
    routes = await db.get_active_routes()
    assert len(routes) == 1
    assert routes[0]["id"] == route_id
    assert routes[0]["from_airport"] == "ATQ"
    assert routes[0]["to_airport"] == "BOM"


@pytest.mark.asyncio
async def test_remove_route(db):
    route_id = await db.add_route("ATQ", "BOM")
    removed = await db.remove_route(route_id)
    assert removed is True
    routes = await db.get_active_routes()
    assert len(routes) == 0


@pytest.mark.asyncio
async def test_remove_nonexistent_route(db):
    removed = await db.remove_route(999)
    assert removed is False


@pytest.mark.asyncio
async def test_save_and_get_price_history(db):
    route_id = await db.add_route("ATQ", "BOM")
    price_data = json.dumps([{"date": "2026-03-18", "price": 3200}])
    await db.save_price_history(
        route_id=route_id,
        scan_date="2026-03-07",
        cheapest_travel_date="2026-03-18",
        cheapest_price=3200.0,
        cheapest_airline="IndiGo",
        avg_price=5200.0,
        price_data=price_data,
    )
    history = await db.get_price_history(route_id, days=7)
    assert len(history) == 1
    assert history[0]["cheapest_price"] == 3200.0


@pytest.mark.asyncio
async def test_add_route_with_stay_days(db):
    route_id = await db.add_route("VIX", "MXP", stay_days=10)
    routes = await db.get_active_routes()
    assert routes[0]["stay_days"] == 10
    assert routes[0]["id"] == route_id


@pytest.mark.asyncio
async def test_save_price_history_with_return_date(db):
    route_id = await db.add_route("VIX", "MXP", stay_days=10)
    await db.save_price_history(
        route_id=route_id,
        scan_date="2026-03-07",
        cheapest_travel_date="2026-03-18",
        cheapest_return_date="2026-03-28",
        cheapest_price=4500.0,
        cheapest_airline="TAP",
        avg_price=5100.0,
        price_data=None,
    )
    history = await db.get_price_history(route_id, days=7)
    assert history[0]["cheapest_return_date"] == "2026-03-28"


@pytest.mark.asyncio
async def test_init_creates_stops_preference(db):
    config = await db.get_config("stops_preference")
    assert config == "any"


@pytest.mark.asyncio
async def test_add_route_default_max_stops(db):
    route_id = await db.add_route("ATQ", "BOM")
    routes = await db.get_active_routes()
    assert routes[0]["max_stops"] is None


@pytest.mark.asyncio
async def test_add_route_with_max_stops(db):
    route_id = await db.add_route("ATQ", "BOM", max_stops="direct")
    routes = await db.get_active_routes()
    assert routes[0]["max_stops"] == "direct"


@pytest.mark.asyncio
async def test_set_route_stops(db):
    route_id = await db.add_route("ATQ", "BOM")
    updated = await db.set_route_stops(route_id, "1stop")
    assert updated is True
    routes = await db.get_active_routes()
    assert routes[0]["max_stops"] == "1stop"


@pytest.mark.asyncio
async def test_set_route_stops_nonexistent(db):
    updated = await db.set_route_stops(999, "direct")
    assert updated is False


@pytest.mark.asyncio
async def test_get_route_stops_preference_per_route(db):
    route_id = await db.add_route("ATQ", "BOM", max_stops="direct")
    pref = await db.get_route_stops_preference(route_id)
    assert pref == "direct"


@pytest.mark.asyncio
async def test_get_route_stops_preference_falls_back_to_global(db):
    route_id = await db.add_route("ATQ", "BOM")
    pref = await db.get_route_stops_preference(route_id)
    assert pref == "any"


@pytest.mark.asyncio
async def test_get_route_stops_preference_custom_global(db):
    route_id = await db.add_route("ATQ", "BOM")
    await db.set_config("stops_preference", "1stop")
    pref = await db.get_route_stops_preference(route_id)
    assert pref == "1stop"


@pytest.mark.asyncio
async def test_init_creates_scan_interval_config(db):
    config = await db.get_config("scan_interval")
    assert config == "1440"


@pytest.mark.asyncio
async def test_routes_have_scan_interval_column(db):
    route_id = await db.add_route("ATQ", "BOM")
    routes = await db.get_active_routes()
    assert routes[0]["scan_interval"] is None


@pytest.mark.asyncio
async def test_set_route_scan_interval(db):
    route_id = await db.add_route("ATQ", "BOM")
    updated = await db.set_route_scan_interval(route_id, "120")
    assert updated is True
    routes = await db.get_active_routes()
    assert routes[0]["scan_interval"] == "120"


@pytest.mark.asyncio
async def test_set_route_scan_interval_invalid(db):
    route_id = await db.add_route("ATQ", "BOM")
    updated = await db.set_route_scan_interval(route_id, "45")
    assert updated is False
    routes = await db.get_active_routes()
    assert routes[0]["scan_interval"] is None


@pytest.mark.asyncio
async def test_set_route_scan_interval_nonexistent(db):
    updated = await db.set_route_scan_interval(999, "120")
    assert updated is False


@pytest.mark.asyncio
async def test_get_route_scan_interval_per_route(db):
    route_id = await db.add_route("ATQ", "BOM")
    await db.set_route_scan_interval(route_id, "120")
    interval = await db.get_route_scan_interval(route_id)
    assert interval == 120


@pytest.mark.asyncio
async def test_get_route_scan_interval_falls_back_to_global(db):
    route_id = await db.add_route("ATQ", "BOM")
    interval = await db.get_route_scan_interval(route_id)
    assert interval == 1440  # default global


@pytest.mark.asyncio
async def test_get_route_scan_interval_custom_global(db):
    route_id = await db.add_route("ATQ", "BOM")
    await db.set_config("scan_interval", "360")
    interval = await db.get_route_scan_interval(route_id)
    assert interval == 360


@pytest.mark.asyncio
async def test_save_price_history_upserts_same_day(db):
    route_id = await db.add_route("ATQ", "BOM")
    # First save
    await db.save_price_history(
        route_id=route_id,
        scan_date="2026-03-07",
        cheapest_travel_date="2026-03-18",
        cheapest_price=3200.0,
        cheapest_airline="IndiGo",
        avg_price=5200.0,
        price_data=None,
    )
    # Second save same day — should overwrite, not duplicate
    await db.save_price_history(
        route_id=route_id,
        scan_date="2026-03-07",
        cheapest_travel_date="2026-03-20",
        cheapest_price=2900.0,
        cheapest_airline="SpiceJet",
        avg_price=4800.0,
        price_data=None,
    )
    history = await db.get_price_history(route_id, days=7)
    assert len(history) == 1
    assert history[0]["cheapest_price"] == 2900.0
    assert history[0]["cheapest_airline"] == "SpiceJet"

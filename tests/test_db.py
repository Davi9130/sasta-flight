import sqlite3

from bot.db import Database
from scripts.import_legacy_db import run as import_legacy


async def test_route_crud_and_preferences(db):
    rid = await db.add_route("vix,gig", "mxp", stay_min=7, stay_max=10)
    route = await db.get_route(rid)
    assert route["from_airport"] == "VIX,GIG" and route["stay_max"] == 10
    assert await db.get_route_stops_preference(route) == "any"
    await db.set_route_stops(rid, "direct")
    assert await db.get_route_stops_preference(await db.get_route(rid)) == "direct"
    assert await db.get_route_scan_interval(route) == 1440
    await db.set_route_scan_interval(rid, "360")
    assert await db.get_route_scan_interval(await db.get_route(rid)) == 360
    assert await db.set_route_alert(rid, target_price=2500)
    assert (await db.get_route(rid))["target_price"] == 2500
    await db.clear_route_alerts(rid)
    assert (await db.get_route(rid))["target_price"] is None
    assert await db.remove_route(rid)
    assert not await db.remove_route(rid)
    assert await db.get_active_routes() == []


async def test_history_snapshots_and_stats(db):
    rid = await db.add_route("VIX", "GRU")
    run_id = await db.create_scan_run(rid, "ok", cheapest_price=900, requests_used=12)
    days = [
        {"date": "2026-12-01", "price": 900, "from_airport": "VIX", "to_airport": "GRU"},
        {"date": "2026-12-02", "price": 950, "from_airport": "VIX", "to_airport": "GRU"},
    ]
    await db.save_fare_snapshots(rid, run_id, days, "BRL")
    for price in (1000, 900):
        await db.save_price_history(
            rid, scan_date="2026-11-03", cheapest_travel_date="2026-12-01", cheapest_return_date=None,
            cheapest_price=price, cheapest_airline="X", avg_price=price, price_data=days,
            currency="BRL", provider="fake", fare_type="oneway",
        )
    history = await db.get_price_history(rid)
    assert len(history) == 1 and history[0]["cheapest_price"] == 900
    stats = await db.get_route_price_stats(rid)
    assert stats["count"] == 1 and stats["min"] == 900
    assert await db.get_previous_cheapest(rid) == 900
    assert await db.get_pair_best_prices(rid) == {("VIX", "GRU"): 900}


async def test_alert_dedupe_and_cooldown(db):
    rid = await db.add_route("VIX", "GRU")
    assert not await db.is_alert_cooling_down(rid, 60)
    assert await db.record_alert_event(rid, "target", 900, "fp1")
    assert not await db.record_alert_event(rid, "target", 900, "fp1")
    assert await db.is_alert_cooling_down(rid, 60)


async def test_import_legacy_db(tmp_path):
    legacy = tmp_path / "flights.db"
    conn = sqlite3.connect(legacy)
    conn.executescript(
        """
        CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE routes (id INTEGER PRIMARY KEY, from_airport TEXT, to_airport TEXT,
            max_stops TEXT, is_active INTEGER, created_at TEXT, scan_interval TEXT,
            stay_days INTEGER, stay_days_max INTEGER, target_price REAL,
            via_hub INTEGER, hub_nights INTEGER, hub_nights_max INTEGER);
        CREATE TABLE price_history (id INTEGER PRIMARY KEY, route_id INTEGER, scan_date TEXT,
            cheapest_travel_date TEXT, cheapest_price REAL, cheapest_airline TEXT,
            avg_price REAL, price_data TEXT, scanned_at TEXT);
        INSERT INTO config VALUES ('notify_time', '07:30');
        INSERT INTO routes VALUES (7, 'VIX', 'MXP', 'direct', 1, '2026-01-01', NULL, 7, 10, 2800, 0, NULL, NULL);
        INSERT INTO price_history VALUES (1, 7, '2026-10-01', '2026-11-01', 3000, 'TAP', 3200, '[]', '2026-10-01');
        """
    )
    conn.commit()
    conn.close()

    target = str(tmp_path / "new.db")
    assert await import_legacy(str(legacy), target) == (1, 1)
    assert await import_legacy(str(legacy), target) == (0, 0)

    db = Database(target)
    await db.init()
    route = await db.get_route(7)
    assert (route["stay_min"], route["stay_max"], route["target_price"]) == (7, 10, 2800)
    assert (await db.get_price_history(7))[0]["cheapest_price"] == 3000
    assert await db.get_config("notify_time") == "07:30"
    await db.close()

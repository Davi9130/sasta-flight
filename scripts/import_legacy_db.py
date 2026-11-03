"""One-shot import of routes, config and price_history from the old Fli-based database.

Usage:
    python -m scripts.import_legacy_db [--legacy data/flights.db] [--target data/sastaflight.db]

Route IDs are preserved so `/alert <id>` keeps working. Running it twice is safe:
rows that already exist in the target are skipped.
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys

from bot.config import DB_PATH
from bot.db import Database

ROUTE_MAP = {
    "from_airport": "from_airport",
    "to_airport": "to_airport",
    "max_stops": "max_stops",
    "scan_interval": "scan_interval",
    "stay_days": "stay_min",
    "stay_days_max": "stay_max",
    "target_price": "target_price",
    "alert_drop_pct": "alert_drop_pct",
    "alert_on_new_low": "alert_on_new_low",
    "alert_cooldown_minutes": "alert_cooldown_minutes",
    "via_hub": "via_hub",
    "hub_nights": "hub_nights_min",
    "hub_nights_max": "hub_nights_max",
    "is_active": "is_active",
    "created_at": "created_at",
}
HISTORY_COLUMNS = (
    "route_id",
    "scan_date",
    "cheapest_travel_date",
    "cheapest_return_date",
    "cheapest_price",
    "cheapest_airline",
    "avg_price",
    "price_data",
    "currency",
    "provider",
    "fare_type",
    "scanned_at",
)
CONFIG_KEYS = ("notify_time", "is_paused", "stops_preference", "scan_interval")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _read_legacy(path: str) -> tuple[list[dict], list[dict], dict[str, str]]:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        route_cols = _columns(conn, "routes")
        select = ", ".join(["id"] + [c for c in ROUTE_MAP if c in route_cols])
        routes = [dict(r) for r in conn.execute(f"SELECT {select} FROM routes")]

        hist_cols = _columns(conn, "price_history")
        hist_select = ", ".join(c for c in HISTORY_COLUMNS if c in hist_cols)
        history = [dict(r) for r in conn.execute(f"SELECT {hist_select} FROM price_history")]

        config = {
            r["key"]: r["value"]
            for r in conn.execute("SELECT key, value FROM config")
            if r["key"] in CONFIG_KEYS
        }
    finally:
        conn.close()
    return routes, history, config


async def run(legacy_path: str, target_path: str) -> tuple[int, int]:
    routes, history, config = _read_legacy(legacy_path)
    db = Database(target_path)
    await db.init()
    try:
        imported_routes = 0
        for route in routes:
            values = {ROUTE_MAP[k]: v for k, v in route.items() if k in ROUTE_MAP}
            values["id"] = route["id"]
            cols = ", ".join(values)
            marks = ", ".join("?" for _ in values)
            cursor = await db.db.execute(
                f"INSERT OR IGNORE INTO routes ({cols}) VALUES ({marks})", tuple(values.values())
            )
            imported_routes += cursor.rowcount

        imported_history = 0
        for row in history:
            cols = ", ".join(row)
            marks = ", ".join("?" for _ in row)
            cursor = await db.db.execute(
                f"INSERT OR IGNORE INTO price_history ({cols}) VALUES ({marks})", tuple(row.values())
            )
            imported_history += cursor.rowcount

        for key, value in config.items():
            await db.db.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
        await db.db.commit()
    finally:
        await db.close()
    return imported_routes, imported_history


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--legacy", default="data/flights.db")
    parser.add_argument("--target", default=DB_PATH)
    args = parser.parse_args(argv)
    if args.legacy == args.target:
        print("Legacy and target databases must be different files.", file=sys.stderr)
        return 2
    routes, history = asyncio.run(run(args.legacy, args.target))
    print(f"Imported {routes} route(s) and {history} price_history row(s) into {args.target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""SQLite persistence: routes, history, alerts, FX, quote cache and anti-block state."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiosqlite

from bot.config import DB_PATH, TIMEZONE

ROUTE_COLUMNS = (
    "id, from_airport, to_airport, max_stops, scan_interval, stay_min, stay_max, "
    "target_price, alert_drop_pct, alert_on_new_low, alert_cooldown_minutes, "
    "via_hub, hub_nights_min, hub_nights_max, is_active, created_at"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS routes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_airport TEXT NOT NULL,
    to_airport TEXT NOT NULL,
    max_stops TEXT DEFAULT NULL,
    scan_interval TEXT DEFAULT NULL,
    stay_min INTEGER DEFAULT NULL,
    stay_max INTEGER DEFAULT NULL,
    target_price REAL DEFAULT NULL,
    alert_drop_pct REAL DEFAULT NULL,
    alert_on_new_low INTEGER DEFAULT 0,
    alert_cooldown_minutes INTEGER DEFAULT NULL,
    via_hub INTEGER DEFAULT 0,
    hub_nights_min INTEGER DEFAULT NULL,
    hub_nights_max INTEGER DEFAULT NULL,
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS scan_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id INTEGER NOT NULL,
    scanned_at TEXT NOT NULL,
    scan_date TEXT NOT NULL,
    status TEXT NOT NULL,
    provider TEXT,
    currency TEXT,
    duration_ms INTEGER,
    error TEXT,
    cheapest_price REAL,
    cheapest_travel_date TEXT,
    cheapest_return_date TEXT,
    fare_type TEXT,
    requests_used INTEGER,
    partial INTEGER DEFAULT 0,
    filters_json TEXT,
    FOREIGN KEY (route_id) REFERENCES routes(id)
);
CREATE INDEX IF NOT EXISTS idx_scan_runs_route ON scan_runs(route_id, scanned_at);

CREATE TABLE IF NOT EXISTS fare_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id INTEGER NOT NULL,
    scan_run_id INTEGER,
    scanned_at TEXT NOT NULL,
    scan_date TEXT NOT NULL,
    from_airport TEXT NOT NULL,
    to_airport TEXT NOT NULL,
    travel_date TEXT NOT NULL,
    return_date TEXT,
    price REAL NOT NULL,
    currency TEXT NOT NULL,
    airline TEXT,
    stops INTEGER,
    duration INTEGER,
    fare_type TEXT,
    is_cheapest INTEGER DEFAULT 0,
    raw_json TEXT,
    FOREIGN KEY (route_id) REFERENCES routes(id)
);
CREATE INDEX IF NOT EXISTS idx_fare_snapshots_route ON fare_snapshots(route_id, scanned_at);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id INTEGER NOT NULL,
    scan_date TEXT NOT NULL,
    cheapest_travel_date TEXT NOT NULL,
    cheapest_return_date TEXT,
    cheapest_price REAL NOT NULL,
    cheapest_airline TEXT,
    avg_price REAL,
    price_data TEXT,
    currency TEXT,
    provider TEXT,
    fare_type TEXT,
    scanned_at TEXT,
    UNIQUE(route_id, scan_date),
    FOREIGN KEY (route_id) REFERENCES routes(id)
);

CREATE TABLE IF NOT EXISTS quote_cache (
    key TEXT PRIMARY KEY,
    fetched_at REAL NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS request_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    outcome TEXT NOT NULL,
    proxy TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_request_log_at ON request_log(at);

CREATE TABLE IF NOT EXISTS guard_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fx_rates (
    base TEXT NOT NULL,
    quote TEXT NOT NULL,
    rate REAL NOT NULL,
    source TEXT,
    as_of_date TEXT,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (base, quote)
);

CREATE TABLE IF NOT EXISTS alert_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id INTEGER NOT NULL,
    rule TEXT NOT NULL,
    price REAL NOT NULL,
    currency TEXT,
    sent_at TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    UNIQUE(route_id, fingerprint),
    FOREIGN KEY (route_id) REFERENCES routes(id)
);

INSERT OR IGNORE INTO config (key, value) VALUES ('notify_time', '08:00');
INSERT OR IGNORE INTO config (key, value) VALUES ('is_paused', '0');
INSERT OR IGNORE INTO config (key, value) VALUES ('stops_preference', 'any');
INSERT OR IGNORE INTO config (key, value) VALUES ('scan_interval', '1440');
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def today_local() -> str:
    return datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d")


def _parse_iso(value: str) -> datetime:
    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


class Database:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self.db: aiosqlite.Connection | None = None

    async def init(self):
        if self.db_path != ":memory:":
            os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self.db = await aiosqlite.connect(self.db_path)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.executescript(SCHEMA)
        await self.db.commit()

    async def close(self):
        if self.db:
            await self.db.close()
            self.db = None

    async def _one(self, sql: str, params: tuple = ()) -> dict | None:
        cursor = await self.db.execute(sql, params)
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        cursor = await self.db.execute(sql, params)
        return [dict(r) for r in await cursor.fetchall()]

    async def _exec(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        cursor = await self.db.execute(sql, params)
        await self.db.commit()
        return cursor

    # --- config ---
    async def get_config(self, key: str) -> str | None:
        row = await self._one("SELECT value FROM config WHERE key = ?", (key,))
        return row["value"] if row else None

    async def set_config(self, key: str, value: str):
        await self._exec("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))

    # --- routes ---
    async def add_route(
        self,
        from_airport: str,
        to_airport: str,
        *,
        max_stops: str | None = None,
        stay_min: int | None = None,
        stay_max: int | None = None,
        via_hub: bool = False,
        hub_nights_min: int | None = None,
        hub_nights_max: int | None = None,
    ) -> int:
        cursor = await self._exec(
            """INSERT INTO routes
            (from_airport, to_airport, max_stops, stay_min, stay_max,
             via_hub, hub_nights_min, hub_nights_max)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                from_airport.upper(),
                to_airport.upper(),
                max_stops,
                stay_min,
                stay_max,
                1 if via_hub else 0,
                hub_nights_min,
                hub_nights_max,
            ),
        )
        return cursor.lastrowid

    async def get_active_routes(self) -> list[dict]:
        return await self._all(f"SELECT {ROUTE_COLUMNS} FROM routes WHERE is_active = 1 ORDER BY id")

    async def get_route(self, route_id: int) -> dict | None:
        return await self._one(f"SELECT {ROUTE_COLUMNS} FROM routes WHERE id = ?", (route_id,))

    async def remove_route(self, route_id: int) -> bool:
        cursor = await self._exec(
            "UPDATE routes SET is_active = 0 WHERE id = ? AND is_active = 1", (route_id,)
        )
        return cursor.rowcount > 0

    async def set_route_stops(self, route_id: int, max_stops: str) -> bool:
        cursor = await self._exec(
            "UPDATE routes SET max_stops = ? WHERE id = ? AND is_active = 1", (max_stops, route_id)
        )
        return cursor.rowcount > 0

    async def get_route_stops_preference(self, route: dict) -> str:
        if route.get("max_stops"):
            return route["max_stops"]
        return await self.get_config("stops_preference") or "any"

    async def set_route_scan_interval(self, route_id: int, interval: str) -> bool:
        cursor = await self._exec(
            "UPDATE routes SET scan_interval = ? WHERE id = ? AND is_active = 1",
            (interval, route_id),
        )
        return cursor.rowcount > 0

    async def get_route_scan_interval(self, route: dict) -> int:
        if route.get("scan_interval"):
            return int(route["scan_interval"])
        value = await self.get_config("scan_interval")
        return int(value) if value else 1440

    async def set_route_alert(
        self,
        route_id: int,
        *,
        target_price: float | None = None,
        alert_drop_pct: float | None = None,
        alert_on_new_low: int | None = None,
        alert_cooldown_minutes: int | None = None,
    ) -> bool:
        cursor = await self._exec(
            """UPDATE routes SET
                target_price = COALESCE(?, target_price),
                alert_drop_pct = COALESCE(?, alert_drop_pct),
                alert_on_new_low = COALESCE(?, alert_on_new_low),
                alert_cooldown_minutes = COALESCE(?, alert_cooldown_minutes)
            WHERE id = ? AND is_active = 1""",
            (target_price, alert_drop_pct, alert_on_new_low, alert_cooldown_minutes, route_id),
        )
        return cursor.rowcount > 0

    async def clear_route_alerts(self, route_id: int) -> bool:
        cursor = await self._exec(
            """UPDATE routes SET target_price = NULL, alert_drop_pct = NULL, alert_on_new_low = 0
            WHERE id = ? AND is_active = 1""",
            (route_id,),
        )
        return cursor.rowcount > 0

    # --- scans & history ---
    async def create_scan_run(
        self,
        route_id: int,
        status: str,
        *,
        provider: str | None = None,
        currency: str | None = None,
        duration_ms: int | None = None,
        error: str | None = None,
        cheapest_price: float | None = None,
        cheapest_travel_date: str | None = None,
        cheapest_return_date: str | None = None,
        fare_type: str | None = None,
        requests_used: int | None = None,
        partial: bool = False,
        filters: dict | None = None,
    ) -> int:
        cursor = await self._exec(
            """INSERT INTO scan_runs
            (route_id, scanned_at, scan_date, status, provider, currency, duration_ms, error,
             cheapest_price, cheapest_travel_date, cheapest_return_date, fare_type,
             requests_used, partial, filters_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                route_id,
                now_iso(),
                today_local(),
                status,
                provider,
                currency,
                duration_ms,
                error,
                cheapest_price,
                cheapest_travel_date,
                cheapest_return_date,
                fare_type,
                requests_used,
                1 if partial else 0,
                json.dumps(filters) if filters is not None else None,
            ),
        )
        return cursor.lastrowid

    async def get_previous_cheapest(self, route_id: int) -> float | None:
        row = await self._one(
            """SELECT cheapest_price FROM scan_runs
            WHERE route_id = ? AND status = 'ok' AND cheapest_price IS NOT NULL
            ORDER BY id DESC LIMIT 1""",
            (route_id,),
        )
        return float(row["cheapest_price"]) if row else None

    async def save_fare_snapshots(
        self, route_id: int, scan_run_id: int, days: list[dict], currency: str
    ):
        scanned_at = now_iso()
        scan_date = today_local()
        rows = [
            (
                route_id,
                scan_run_id,
                scanned_at,
                scan_date,
                day.get("from_airport") or "",
                day.get("to_airport") or "",
                day["date"],
                day.get("return_date"),
                day["price"],
                currency,
                day.get("airline"),
                day.get("stops"),
                day.get("duration"),
                day.get("fare_type"),
                1 if i == 0 else 0,
                json.dumps(day, default=str),
            )
            for i, day in enumerate(days)
        ]
        await self.db.executemany(
            """INSERT INTO fare_snapshots
            (route_id, scan_run_id, scanned_at, scan_date, from_airport, to_airport,
             travel_date, return_date, price, currency, airline, stops, duration,
             fare_type, is_cheapest, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        await self.db.commit()

    async def save_price_history(
        self,
        route_id: int,
        *,
        scan_date: str,
        cheapest_travel_date: str,
        cheapest_return_date: str | None,
        cheapest_price: float,
        cheapest_airline: str | None,
        avg_price: float | None,
        price_data: list[dict] | None,
        currency: str | None,
        provider: str | None,
        fare_type: str | None,
    ):
        """Daily upsert: the last scan of the day wins."""
        await self._exec(
            """INSERT INTO price_history
            (route_id, scan_date, cheapest_travel_date, cheapest_return_date, cheapest_price,
             cheapest_airline, avg_price, price_data, currency, provider, fare_type, scanned_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(route_id, scan_date) DO UPDATE SET
                cheapest_travel_date=excluded.cheapest_travel_date,
                cheapest_return_date=excluded.cheapest_return_date,
                cheapest_price=excluded.cheapest_price,
                cheapest_airline=excluded.cheapest_airline,
                avg_price=excluded.avg_price,
                price_data=excluded.price_data,
                currency=excluded.currency,
                provider=excluded.provider,
                fare_type=excluded.fare_type,
                scanned_at=excluded.scanned_at""",
            (
                route_id,
                scan_date,
                cheapest_travel_date,
                cheapest_return_date,
                cheapest_price,
                cheapest_airline,
                avg_price,
                json.dumps(price_data, default=str) if price_data is not None else None,
                currency,
                provider,
                fare_type,
                now_iso(),
            ),
        )

    async def get_price_history(self, route_id: int, days: int = 14) -> list[dict]:
        """Newest first."""
        return await self._all(
            """SELECT scan_date, cheapest_travel_date, cheapest_return_date, cheapest_price,
                      cheapest_airline, avg_price, currency, provider, fare_type, scanned_at
            FROM price_history WHERE route_id = ?
            ORDER BY scan_date DESC LIMIT ?""",
            (route_id, days),
        )

    async def get_route_price_stats(self, route_id: int, days: int = 30) -> dict:
        rows = await self._all(
            """SELECT price FROM fare_snapshots
            WHERE route_id = ? AND is_cheapest = 1 AND scanned_at >= ?
            ORDER BY scanned_at ASC""",
            (route_id, _days_ago_iso(days)),
        )
        prices = [float(r["price"]) for r in rows]
        if not prices:
            hist = await self.get_price_history(route_id, days=days)
            prices = [float(h["cheapest_price"]) for h in reversed(hist)]
        if not prices:
            return {"count": 0, "min": None, "max": None, "median": None, "latest": None, "prices": []}
        return {
            "count": len(prices),
            "min": min(prices),
            "max": max(prices),
            "median": _median(prices),
            "latest": prices[-1],
            "prices": prices,
        }

    async def get_pair_best_prices(self, route_id: int, days: int = 30) -> dict[tuple[str, str], float]:
        """Cheapest price seen per (from, to) pair; used to prioritise multi-airport probes."""
        rows = await self._all(
            """SELECT from_airport, to_airport, MIN(price) AS best FROM fare_snapshots
            WHERE route_id = ? AND scanned_at >= ?
            GROUP BY from_airport, to_airport""",
            (route_id, _days_ago_iso(days)),
        )
        return {(r["from_airport"], r["to_airport"]): float(r["best"]) for r in rows}

    # --- quote cache ---
    async def cache_get(self, key: str, min_fetched_at: float) -> str | None:
        row = await self._one(
            "SELECT payload FROM quote_cache WHERE key = ? AND fetched_at >= ?",
            (key, min_fetched_at),
        )
        return row["payload"] if row else None

    async def cache_put(self, key: str, fetched_at: float, payload: str):
        await self._exec(
            "INSERT OR REPLACE INTO quote_cache (key, fetched_at, payload) VALUES (?, ?, ?)",
            (key, fetched_at, payload),
        )

    async def cache_purge(self, older_than: float) -> int:
        cursor = await self._exec("DELETE FROM quote_cache WHERE fetched_at < ?", (older_than,))
        return cursor.rowcount

    # --- anti-block state ---
    async def log_request(self, at: float, outcome: str, proxy: str | None, detail: str | None):
        await self._exec(
            "INSERT INTO request_log (at, outcome, proxy, detail) VALUES (?, ?, ?, ?)",
            (at, outcome, proxy, detail),
        )

    async def request_times_since(self, since: float) -> list[float]:
        rows = await self._all(
            "SELECT at FROM request_log WHERE at >= ? ORDER BY at ASC", (since,)
        )
        return [float(r["at"]) for r in rows]

    async def last_request_at(self) -> float | None:
        row = await self._one("SELECT MAX(at) AS at FROM request_log")
        return float(row["at"]) if row and row["at"] is not None else None

    async def purge_request_log(self, older_than: float):
        await self._exec("DELETE FROM request_log WHERE at < ?", (older_than,))

    async def get_guard(self, key: str) -> str | None:
        row = await self._one("SELECT value FROM guard_state WHERE key = ?", (key,))
        return row["value"] if row else None

    async def set_guard(self, key: str, value: str):
        await self._exec(
            "INSERT OR REPLACE INTO guard_state (key, value) VALUES (?, ?)", (key, value)
        )

    # --- FX ---
    async def save_fx_rate(self, base: str, quote: str, rate: float, source: str | None = None):
        await self._exec(
            """INSERT INTO fx_rates (base, quote, rate, source, as_of_date, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(base, quote) DO UPDATE SET
                rate=excluded.rate, source=excluded.source,
                as_of_date=excluded.as_of_date, fetched_at=excluded.fetched_at""",
            (base, quote, rate, source, datetime.now(timezone.utc).strftime("%Y-%m-%d"), now_iso()),
        )

    async def get_fx_rate(self, base: str, quote: str) -> dict | None:
        return await self._one(
            "SELECT base, quote, rate, source, as_of_date, fetched_at FROM fx_rates WHERE base = ? AND quote = ?",
            (base, quote),
        )

    # --- alerts ---
    async def record_alert_event(
        self, route_id: int, rule: str, price: float, fingerprint: str, currency: str | None = None
    ) -> bool:
        """False when the fingerprint was already sent (dedupe)."""
        try:
            await self._exec(
                """INSERT INTO alert_events (route_id, rule, price, currency, sent_at, fingerprint)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (route_id, rule, price, currency, now_iso(), fingerprint),
            )
            return True
        except aiosqlite.IntegrityError:
            return False

    async def is_alert_cooling_down(self, route_id: int, cooldown_minutes: int) -> bool:
        row = await self._one(
            "SELECT sent_at FROM alert_events WHERE route_id = ? ORDER BY id DESC LIMIT 1",
            (route_id,),
        )
        if not row:
            return False
        try:
            age = (datetime.now(timezone.utc) - _parse_iso(row["sent_at"])).total_seconds() / 60
        except (ValueError, TypeError):
            return False
        return age < cooldown_minutes


def _days_ago_iso(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

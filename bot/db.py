import json
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import aiosqlite

from bot.config import DB_PATH, TIMEZONE


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_local() -> str:
    return datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d")


class Database:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self.db: aiosqlite.Connection | None = None

    async def init(self):
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self.db = await aiosqlite.connect(self.db_path)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA foreign_keys = ON")
        await self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS routes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_airport TEXT NOT NULL,
                to_airport TEXT NOT NULL,
                max_stops TEXT DEFAULT NULL,
                is_active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                route_id INTEGER NOT NULL,
                scan_date TEXT NOT NULL,
                cheapest_travel_date TEXT NOT NULL,
                cheapest_price REAL NOT NULL,
                cheapest_airline TEXT,
                avg_price REAL,
                price_data TEXT,
                scanned_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (route_id) REFERENCES routes(id)
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_price_history_route_date
            ON price_history(route_id, scan_date);

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
                candidates_checked INTEGER,
                filters_json TEXT,
                FOREIGN KEY (route_id) REFERENCES routes(id)
            );

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
                FOREIGN KEY (route_id) REFERENCES routes(id),
                FOREIGN KEY (scan_run_id) REFERENCES scan_runs(id)
            );

            CREATE INDEX IF NOT EXISTS idx_fare_snapshots_route_scanned
            ON fare_snapshots(route_id, scanned_at DESC);

            CREATE TABLE IF NOT EXISTS fx_rates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                base TEXT NOT NULL,
                quote TEXT NOT NULL,
                rate REAL NOT NULL,
                source TEXT,
                as_of_date TEXT,
                fetched_at TEXT NOT NULL,
                UNIQUE(base, quote)
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
            INSERT OR IGNORE INTO config (key, value) VALUES ('always_send_summary', '0');
            """
        )
        await self.db.commit()
        await self._migrate()

    async def _migrate(self):
        columns = {
            "routes": {
                "max_stops": "TEXT DEFAULT NULL",
                "scan_interval": "TEXT DEFAULT NULL",
                "stay_days": "INTEGER DEFAULT NULL",
                "stay_days_max": "INTEGER DEFAULT NULL",
                "target_price": "REAL DEFAULT NULL",
                "alert_drop_pct": "REAL DEFAULT NULL",
                "alert_on_new_low": "INTEGER DEFAULT 0",
                "alert_cooldown_minutes": "INTEGER DEFAULT NULL",
            },
            "price_history": {
                "cheapest_return_date": "TEXT DEFAULT NULL",
                "currency": "TEXT DEFAULT NULL",
                "provider": "TEXT DEFAULT NULL",
                "fare_type": "TEXT DEFAULT NULL",
            },
        }
        for table, cols in columns.items():
            existing = await self._table_columns(table)
            for col, decl in cols.items():
                if col not in existing:
                    try:
                        await self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                        await self.db.commit()
                    except aiosqlite.Error:
                        pass

    async def _table_columns(self, table: str) -> set[str]:
        cursor = await self.db.execute(f"PRAGMA table_info({table})")
        rows = await cursor.fetchall()
        return {row["name"] for row in rows}

    async def close(self):
        if self.db:
            await self.db.close()

    async def get_config(self, key: str) -> str | None:
        cursor = await self.db.execute(
            "SELECT value FROM config WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        return row["value"] if row else None

    async def set_config(self, key: str, value: str):
        await self.db.execute(
            "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
            (key, value),
        )
        await self.db.commit()

    async def add_route(
        self,
        from_airport: str,
        to_airport: str,
        max_stops: str | None = None,
        stay_days: int | None = None,
        stay_days_max: int | None = None,
    ) -> int:
        cursor = await self.db.execute(
            """INSERT INTO routes
            (from_airport, to_airport, max_stops, stay_days, stay_days_max)
            VALUES (?, ?, ?, ?, ?)""",
            (
                from_airport.upper(),
                to_airport.upper(),
                max_stops,
                stay_days,
                stay_days_max,
            ),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def get_active_routes(self) -> list[dict]:
        cursor = await self.db.execute(
            """SELECT id, from_airport, to_airport, max_stops, scan_interval,
                      stay_days, stay_days_max, target_price, alert_drop_pct,
                      alert_on_new_low, alert_cooldown_minutes
            FROM routes WHERE is_active = 1"""
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_route(self, route_id: int) -> dict | None:
        cursor = await self.db.execute(
            """SELECT id, from_airport, to_airport, max_stops, scan_interval,
                      stay_days, stay_days_max, target_price, alert_drop_pct,
                      alert_on_new_low, alert_cooldown_minutes, is_active
            FROM routes WHERE id = ?""",
            (route_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def remove_route(self, route_id: int) -> bool:
        cursor = await self.db.execute(
            "UPDATE routes SET is_active = 0 WHERE id = ? AND is_active = 1",
            (route_id,),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def set_route_stops(self, route_id: int, max_stops: str) -> bool:
        cursor = await self.db.execute(
            "UPDATE routes SET max_stops = ? WHERE id = ? AND is_active = 1",
            (max_stops, route_id),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def get_route_stops_preference(self, route_id: int) -> str:
        cursor = await self.db.execute(
            "SELECT max_stops FROM routes WHERE id = ?", (route_id,)
        )
        row = await cursor.fetchone()
        if row and row["max_stops"]:
            return row["max_stops"]
        return await self.get_config("stops_preference") or "any"

    async def set_route_scan_interval(self, route_id: int, interval: str) -> bool:
        valid = {"60", "120", "240", "360", "720", "1440"}
        if interval not in valid:
            return False
        cursor = await self.db.execute(
            "UPDATE routes SET scan_interval = ? WHERE id = ? AND is_active = 1",
            (interval, route_id),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def get_route_scan_interval(self, route_id: int) -> int:
        cursor = await self.db.execute(
            "SELECT scan_interval FROM routes WHERE id = ?", (route_id,)
        )
        row = await cursor.fetchone()
        if row and row["scan_interval"]:
            return int(row["scan_interval"])
        global_interval = await self.get_config("scan_interval")
        return int(global_interval) if global_interval else 1440

    async def set_route_alert(
        self,
        route_id: int,
        *,
        target_price: float | None = None,
        alert_drop_pct: float | None = None,
        alert_on_new_low: int | None = None,
        alert_cooldown_minutes: int | None = None,
    ) -> bool:
        route = await self.get_route(route_id)
        if not route or not route.get("is_active", 1):
            return False
        await self.db.execute(
            """UPDATE routes SET
                target_price = COALESCE(?, target_price),
                alert_drop_pct = COALESCE(?, alert_drop_pct),
                alert_on_new_low = COALESCE(?, alert_on_new_low),
                alert_cooldown_minutes = COALESCE(?, alert_cooldown_minutes)
            WHERE id = ? AND is_active = 1""",
            (
                target_price,
                alert_drop_pct,
                alert_on_new_low,
                alert_cooldown_minutes,
                route_id,
            ),
        )
        await self.db.commit()
        return True

    async def clear_route_target_price(self, route_id: int) -> bool:
        cursor = await self.db.execute(
            "UPDATE routes SET target_price = NULL WHERE id = ? AND is_active = 1",
            (route_id,),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def save_price_history(
        self,
        route_id: int,
        scan_date: str,
        cheapest_travel_date: str,
        cheapest_price: float,
        cheapest_airline: str | None,
        avg_price: float | None,
        price_data: str | None,
        cheapest_return_date: str | None = None,
        currency: str | None = None,
        provider: str | None = None,
        fare_type: str | None = None,
    ):
        """Legacy daily upsert kept for compatibility with /history charts."""
        await self.db.execute(
            """INSERT INTO price_history
            (route_id, scan_date, cheapest_travel_date, cheapest_return_date,
             cheapest_price, cheapest_airline, avg_price, price_data,
             currency, provider, fare_type, scanned_at)
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
                scanned_at=excluded.scanned_at
            """,
            (
                route_id,
                scan_date,
                cheapest_travel_date,
                cheapest_return_date,
                cheapest_price,
                cheapest_airline,
                avg_price,
                price_data,
                currency,
                provider,
                fare_type,
                _now_iso(),
            ),
        )
        await self.db.commit()

    async def get_price_history(self, route_id: int, days: int = 7) -> list[dict]:
        """Return up to `days` daily history rows (legacy chart)."""
        cursor = await self.db.execute(
            """SELECT scan_date, cheapest_travel_date, cheapest_return_date,
                      cheapest_price, cheapest_airline, avg_price, price_data,
                      currency, provider, fare_type, scanned_at
            FROM price_history
            WHERE route_id = ?
            ORDER BY scan_date DESC
            LIMIT ?""",
            (route_id, days),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_previous_cheapest(self, route_id: int) -> float | None:
        """Previous confirmed cheapest from scan_runs (not same run)."""
        cursor = await self.db.execute(
            """SELECT cheapest_price FROM scan_runs
            WHERE route_id = ? AND status = 'ok' AND cheapest_price IS NOT NULL
            ORDER BY scanned_at DESC
            LIMIT 1""",
            (route_id,),
        )
        row = await cursor.fetchone()
        return float(row["cheapest_price"]) if row else None

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
        candidates_checked: int | None = None,
        filters_json: str | None = None,
    ) -> int:
        scanned_at = _now_iso()
        scan_date = _today_local()
        cursor = await self.db.execute(
            """INSERT INTO scan_runs
            (route_id, scanned_at, scan_date, status, provider, currency,
             duration_ms, error, cheapest_price, cheapest_travel_date,
             cheapest_return_date, fare_type, candidates_checked, filters_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                route_id,
                scanned_at,
                scan_date,
                status,
                provider,
                currency,
                duration_ms,
                error,
                cheapest_price,
                cheapest_travel_date,
                cheapest_return_date,
                fare_type,
                candidates_checked,
                filters_json,
            ),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def save_fare_snapshots(
        self,
        route_id: int,
        scan_run_id: int,
        snapshots: list[dict],
        currency: str,
    ):
        scanned_at = _now_iso()
        scan_date = _today_local()
        rows = []
        for snap in snapshots:
            rows.append(
                (
                    route_id,
                    scan_run_id,
                    scanned_at,
                    scan_date,
                    snap.get("from_airport") or "",
                    snap.get("to_airport") or "",
                    snap["date"],
                    snap.get("return_date"),
                    snap["price"],
                    currency,
                    snap.get("airline"),
                    snap.get("stops"),
                    snap.get("duration"),
                    snap.get("fare_type"),
                    1 if snap.get("is_cheapest") else 0,
                    json.dumps(snap),
                )
            )
        await self.db.executemany(
            """INSERT INTO fare_snapshots
            (route_id, scan_run_id, scanned_at, scan_date, from_airport, to_airport,
             travel_date, return_date, price, currency, airline, stops, duration,
             fare_type, is_cheapest, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        await self.db.commit()

    async def get_fare_history(
        self, route_id: int, *, hours: int | None = None, limit: int = 50
    ) -> list[dict]:
        if hours is not None:
            cursor = await self.db.execute(
                """SELECT scanned_at, scan_date, from_airport, to_airport,
                          travel_date, return_date, price, currency, airline,
                          fare_type, is_cheapest
                FROM fare_snapshots
                WHERE route_id = ? AND is_cheapest = 1
                  AND scanned_at >= datetime('now', ?)
                ORDER BY scanned_at DESC
                LIMIT ?""",
                (route_id, f"-{hours} hours", limit),
            )
        else:
            cursor = await self.db.execute(
                """SELECT scanned_at, scan_date, from_airport, to_airport,
                          travel_date, return_date, price, currency, airline,
                          fare_type, is_cheapest
                FROM fare_snapshots
                WHERE route_id = ? AND is_cheapest = 1
                ORDER BY scanned_at DESC
                LIMIT ?""",
                (route_id, limit),
            )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_route_price_stats(self, route_id: int, days: int = 30) -> dict:
        cursor = await self.db.execute(
            """SELECT price FROM fare_snapshots
            WHERE route_id = ? AND is_cheapest = 1
              AND scanned_at >= datetime('now', ?)
            ORDER BY scanned_at ASC""",
            (route_id, f"-{days} days"),
        )
        rows = await cursor.fetchall()
        prices = [float(r["price"]) for r in rows]
        if not prices:
            # Fallback to legacy daily history
            hist = await self.get_price_history(route_id, days=days)
            prices = [float(h["cheapest_price"]) for h in reversed(hist)]
        if not prices:
            return {"count": 0, "min": None, "max": None, "median": None, "latest": None}
        sorted_prices = sorted(prices)
        mid = len(sorted_prices) // 2
        if len(sorted_prices) % 2:
            median = sorted_prices[mid]
        else:
            median = (sorted_prices[mid - 1] + sorted_prices[mid]) / 2
        return {
            "count": len(prices),
            "min": min(prices),
            "max": max(prices),
            "median": median,
            "latest": prices[-1],
            "prices": prices,
        }

    async def save_fx_rate(
        self,
        base: str,
        quote: str,
        rate: float,
        source: str | None = None,
        as_of_date: str | None = None,
    ):
        await self.db.execute(
            """INSERT INTO fx_rates (base, quote, rate, source, as_of_date, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(base, quote) DO UPDATE SET
                rate=excluded.rate,
                source=excluded.source,
                as_of_date=excluded.as_of_date,
                fetched_at=excluded.fetched_at
            """,
            (base, quote, rate, source, as_of_date, _now_iso()),
        )
        await self.db.commit()

    async def get_fx_rate(self, base: str, quote: str) -> dict | None:
        cursor = await self.db.execute(
            """SELECT base, quote, rate, source, as_of_date, fetched_at
            FROM fx_rates WHERE base = ? AND quote = ?""",
            (base, quote),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def record_alert_event(
        self,
        route_id: int,
        rule: str,
        price: float,
        fingerprint: str,
        currency: str | None = None,
    ) -> bool:
        """Insert alert event. Returns False if fingerprint already exists (dedupe)."""
        try:
            await self.db.execute(
                """INSERT INTO alert_events
                (route_id, rule, price, currency, sent_at, fingerprint)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (route_id, rule, price, currency, _now_iso(), fingerprint),
            )
            await self.db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def get_last_alert(self, route_id: int, rule: str | None = None) -> dict | None:
        if rule:
            cursor = await self.db.execute(
                """SELECT route_id, rule, price, currency, sent_at, fingerprint
                FROM alert_events WHERE route_id = ? AND rule = ?
                ORDER BY sent_at DESC LIMIT 1""",
                (route_id, rule),
            )
        else:
            cursor = await self.db.execute(
                """SELECT route_id, rule, price, currency, sent_at, fingerprint
                FROM alert_events WHERE route_id = ?
                ORDER BY sent_at DESC LIMIT 1""",
                (route_id,),
            )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def is_alert_cooling_down(
        self, route_id: int, cooldown_minutes: int, rule: str | None = None
    ) -> bool:
        last = await self.get_last_alert(route_id, rule=rule)
        if not last:
            return False
        try:
            sent_at = datetime.fromisoformat(last["sent_at"].replace("Z", "+00:00"))
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - sent_at).total_seconds() / 60
            return age < cooldown_minutes
        except (ValueError, TypeError, KeyError):
            return False

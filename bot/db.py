import aiosqlite
import os

from bot.config import DB_PATH


class Database:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self.db: aiosqlite.Connection | None = None

    async def init(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.db = await aiosqlite.connect(self.db_path)
        self.db.row_factory = aiosqlite.Row
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

            INSERT OR IGNORE INTO config (key, value) VALUES ('notify_time', '08:00');
            INSERT OR IGNORE INTO config (key, value) VALUES ('is_paused', '0');
            INSERT OR IGNORE INTO config (key, value) VALUES ('stops_preference', 'any');
            INSERT OR IGNORE INTO config (key, value) VALUES ('scan_interval', '1440');
            """
        )
        await self.db.commit()

        # Migrate: add max_stops column if missing
        try:
            await self.db.execute("ALTER TABLE routes ADD COLUMN max_stops TEXT DEFAULT NULL")
            await self.db.commit()
        except Exception:
            pass  # Column already exists

        # Migrate: add scan_interval column if missing
        try:
            await self.db.execute("ALTER TABLE routes ADD COLUMN scan_interval TEXT DEFAULT NULL")
            await self.db.commit()
        except Exception:
            pass  # Column already exists

        # Migrate: add stay_days column if missing
        try:
            await self.db.execute("ALTER TABLE routes ADD COLUMN stay_days INTEGER DEFAULT NULL")
            await self.db.commit()
        except Exception:
            pass  # Column already exists

        # Migrate: add cheapest_return_date column if missing
        try:
            await self.db.execute(
                "ALTER TABLE price_history ADD COLUMN cheapest_return_date TEXT DEFAULT NULL"
            )
            await self.db.commit()
        except Exception:
            pass  # Column already exists

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
    ) -> int:
        cursor = await self.db.execute(
            "INSERT INTO routes (from_airport, to_airport, max_stops, stay_days) VALUES (?, ?, ?, ?)",
            (from_airport.upper(), to_airport.upper(), max_stops, stay_days),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def get_active_routes(self) -> list[dict]:
        cursor = await self.db.execute(
            """SELECT id, from_airport, to_airport, max_stops, scan_interval, stay_days
            FROM routes WHERE is_active = 1"""
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

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
        """Return effective scan interval in minutes for a route."""
        cursor = await self.db.execute(
            "SELECT scan_interval FROM routes WHERE id = ?", (route_id,)
        )
        row = await cursor.fetchone()
        if row and row["scan_interval"]:
            return int(row["scan_interval"])
        global_interval = await self.get_config("scan_interval")
        return int(global_interval) if global_interval else 1440

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
    ):
        await self.db.execute(
            """INSERT OR REPLACE INTO price_history
            (route_id, scan_date, cheapest_travel_date, cheapest_return_date,
             cheapest_price, cheapest_airline, avg_price, price_data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                route_id,
                scan_date,
                cheapest_travel_date,
                cheapest_return_date,
                cheapest_price,
                cheapest_airline,
                avg_price,
                price_data,
            ),
        )
        await self.db.commit()

    async def get_price_history(self, route_id: int, days: int = 7) -> list[dict]:
        cursor = await self.db.execute(
            """SELECT scan_date, cheapest_travel_date, cheapest_return_date,
                      cheapest_price, cheapest_airline, avg_price, price_data
            FROM price_history
            WHERE route_id = ?
            ORDER BY scan_date DESC
            LIMIT ?""",
            (route_id, days),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

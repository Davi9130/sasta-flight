from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from bot.db import Database
from bot.fares.cache import QuoteCache
from bot.fares.client import FareClient
from bot.fares.guard import Guard
from bot.fares.service import FareService
from bot.fx import FxService

if TYPE_CHECKING:
    from bot.scheduler import ScanQueue

SERVICES_KEY = "svc"


@dataclass
class Services:
    db: Database
    fares: FareService
    fx: FxService | None = None
    queue: ScanQueue | None = None


async def build_services(db_path: str | None = None) -> Services:
    db = Database(db_path) if db_path else Database()
    await db.init()
    fares = FareService(FareClient(), Guard(db), QuoteCache(db))
    return Services(db=db, fares=fares, fx=FxService(db))


def get_services(application) -> Services:
    return application.bot_data[SERVICES_KEY]

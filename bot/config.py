import logging
import os
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
DAYS_TO_SCAN = int(os.getenv("DAYS_TO_SCAN", "30"))
TOP_CHEAPEST = int(os.getenv("TOP_CHEAPEST", "5"))
TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata")
DB_PATH = os.getenv("DB_PATH", "data/flights.db")

SUPPORTED_CURRENCIES = {"BRL", "USD", "EUR", "GBP"}

CURRENCY_COUNTRY = {
    "BRL": "BR",
    "USD": "US",
    "EUR": "DE",
    "GBP": "GB",
}

CURRENCY_SYMBOL = {
    "BRL": "R$",
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
}

_raw_currency = os.getenv("CURRENCY", "BRL").upper()
if _raw_currency not in SUPPORTED_CURRENCIES:
    logger.warning("Invalid CURRENCY=%r, falling back to BRL", _raw_currency)
    CURRENCY = "BRL"
else:
    CURRENCY = _raw_currency
COUNTRY = CURRENCY_COUNTRY[CURRENCY]

MIN_STAY_DAYS = 1
MAX_STAY_DAYS = 90

INTERVAL_OPTIONS = {"1h": 60, "2h": 120, "4h": 240, "6h": 360, "12h": 720, "24h": 1440}

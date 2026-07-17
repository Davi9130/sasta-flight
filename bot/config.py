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

# Search strategy
CANDIDATE_POOL = int(os.getenv("CANDIDATE_POOL", "12"))
MAX_CONCURRENT_SEARCHES = int(os.getenv("MAX_CONCURRENT_SEARCHES", "3"))
SEARCH_TIMEOUT_SECS = float(os.getenv("SEARCH_TIMEOUT_SECS", "45"))
MAX_AIRPORT_COMBOS = int(os.getenv("MAX_AIRPORT_COMBOS", "6"))
ENABLE_SPLIT_TICKETS = os.getenv("ENABLE_SPLIT_TICKETS", "1") != "0"
SEARCH_CACHE_TTL_SECS = int(os.getenv("SEARCH_CACHE_TTL_SECS", "900"))

# Alerts
DEFAULT_ALERT_DROP_PCT = float(os.getenv("DEFAULT_ALERT_DROP_PCT", "5"))
DEFAULT_ALERT_COOLDOWN_MINUTES = int(os.getenv("DEFAULT_ALERT_COOLDOWN_MINUTES", "360"))
ALWAYS_SEND_SCAN_SUMMARY = os.getenv("ALWAYS_SEND_SCAN_SUMMARY", "1") != "0"

# FX
FX_CACHE_HOURS = int(os.getenv("FX_CACHE_HOURS", "24"))
FX_API_URL = os.getenv("FX_API_URL", "https://api.frankfurter.dev/v2")
DISPLAY_CURRENCIES = ("EUR", "USD")

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

FARE_PROVIDER = os.getenv("FARE_PROVIDER", "fli")

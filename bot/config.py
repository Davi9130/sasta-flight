import logging
import os

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, "1" if default else "0").strip().lower() not in {"0", "false", "no", ""}


BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
TIMEZONE = os.getenv("TIMEZONE", "America/Sao_Paulo")
DB_PATH = os.getenv("DB_PATH", "data/sastaflight.db")

DAYS_TO_SCAN = _int("DAYS_TO_SCAN", 30)
TOP_CHEAPEST = _int("TOP_CHEAPEST", 5)
MAX_AIRPORT_COMBOS = _int("MAX_AIRPORT_COMBOS", 4)
ENABLE_SPLIT_TICKETS = _bool("ENABLE_SPLIT_TICKETS", False)
SPLIT_CHECK_TOP = _int("SPLIT_CHECK_TOP", 2)
ALWAYS_SEND_SCAN_SUMMARY = _bool("ALWAYS_SEND_SCAN_SUMMARY", False)

# --- Anti-block: every Google request goes through the guard ---
PROXY_URLS = [p.strip() for p in os.getenv("PROXY_URLS", "").split(",") if p.strip()]
_HAS_PROXY = bool(PROXY_URLS)
REQ_BUDGET_PER_SCAN = _int("REQ_BUDGET_PER_SCAN", 40 if _HAS_PROXY else 25)
REQ_BUDGET_PER_HOUR = _int("REQ_BUDGET_PER_HOUR", 200 if _HAS_PROXY else 60)
REQ_BUDGET_PER_DAY = _int("REQ_BUDGET_PER_DAY", 2000 if _HAS_PROXY else 400)
REQ_JITTER_MIN_SECS = _float("REQ_JITTER_MIN_SECS", 2.0 if _HAS_PROXY else 4.0)
REQ_JITTER_MAX_SECS = _float("REQ_JITTER_MAX_SECS", 5.0 if _HAS_PROXY else 9.0)
REQ_TIMEOUT_SECS = _float("REQ_TIMEOUT_SECS", 30)
REQ_ERROR_RETRIES = _int("REQ_ERROR_RETRIES", 1)
BLOCK_COOLDOWN_SECS = _float("BLOCK_COOLDOWN_SECS", 1800)
BLOCK_COOLDOWN_MAX_SECS = _float("BLOCK_COOLDOWN_MAX_SECS", 6 * 3600)
PROXY_QUARANTINE_SECS = _float("PROXY_QUARANTINE_SECS", 1800)
QUOTE_CACHE_TTL_MINS = _int("QUOTE_CACHE_TTL_MINS", 180)
SCHEDULE_JITTER_MINS = _float("SCHEDULE_JITTER_MINS", 10)

# --- Date sampling (fast-flights searches one date per request) ---
COARSE_STEP_DAYS = max(1, _int("COARSE_STEP_DAYS", 3))
REFINE_TOP_K = max(1, _int("REFINE_TOP_K", 3))
REFINE_RADIUS_DAYS = max(1, _int("REFINE_RADIUS_DAYS", 2))
COARSE_BUDGET_SHARE = _float("COARSE_BUDGET_SHARE", 0.6)
VIA_BUDGET_PER_SCAN = _int("VIA_BUDGET_PER_SCAN", 60 if _HAS_PROXY else 40)
VIA_LONGHAUL_HUBS = [
    c.strip().upper() for c in os.getenv("VIA_LONGHAUL_HUBS", "GRU,GIG").split(",") if c.strip()
]

# --- Alerts ---
DEFAULT_ALERT_DROP_PCT = _float("DEFAULT_ALERT_DROP_PCT", 5)
DEFAULT_ALERT_COOLDOWN_MINUTES = _int("DEFAULT_ALERT_COOLDOWN_MINUTES", 360)

# --- FX ---
FX_CACHE_HOURS = _int("FX_CACHE_HOURS", 24)
FX_API_URL = os.getenv("FX_API_URL", "https://api.frankfurter.dev/v2")
DISPLAY_CURRENCIES = ("EUR", "USD")

SUPPORTED_CURRENCIES = {"BRL", "USD", "EUR", "GBP"}
CURRENCY_SYMBOL = {"BRL": "R$", "USD": "$", "EUR": "€", "GBP": "£"}

_raw_currency = os.getenv("CURRENCY", "BRL").upper()
if _raw_currency not in SUPPORTED_CURRENCIES:
    logger.warning("Invalid CURRENCY=%r, falling back to BRL", _raw_currency)
    CURRENCY = "BRL"
else:
    CURRENCY = _raw_currency
LANGUAGE = os.getenv("LANGUAGE", "en-US")

MIN_STAY_DAYS = 1
MAX_STAY_DAYS = 90
MAX_HUB_NIGHTS = 14
MIN_SELF_TRANSFER_HOURS = _float("MIN_SELF_TRANSFER_HOURS", 4)

INTERVAL_OPTIONS = {"1h": 60, "2h": 120, "4h": 240, "6h": 360, "12h": 720, "24h": 1440}
STOPS_OPTIONS = ("any", "direct", "1stop", "2stops")
STOPS_MAX = {"any": None, "direct": 0, "1stop": 1, "2stops": 2}

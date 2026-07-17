# SastaFlight ✈️

Daily flight price scanner Telegram bot. Scans Google Flights for the cheapest days to fly on your routes, tracks price history, and alerts you on deals.

**What it does:** On a schedule (or on demand), you get a Telegram message with the cheapest confirmed days in the next N days for each saved route — with prices in your main currency plus ≈ EUR / USD, airlines, trends, and booking links.

## Quick Start

### 1. Create a Telegram Bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow the prompts
3. Copy the bot token

### 2. Get Your Chat ID

1. Message [@userinfobot](https://t.me/userinfobot) on Telegram
2. Copy the `Id` number

### 3. Deploy

#### Option A: Railway

1. Go to [railway.com](https://railway.com) and sign up / log in
2. Click **New Project** → **GitHub Repository**
3. Connect your GitHub and select `sasta-flight`
4. Go to **Variables** tab and add:
   - `TELEGRAM_BOT_TOKEN` = your bot token
   - `TELEGRAM_CHAT_ID` = your chat ID
5. Railway will build and deploy automatically
6. (Optional) Add a volume mounted at `/app/data` for persistent database storage

#### Option B: DigitalOcean App Platform

1. Go to [cloud.digitalocean.com/apps](https://cloud.digitalocean.com/apps) and sign up / log in
2. Click **Create App** → **GitHub** → select `sasta-flight`
3. Choose **Worker** (not web service, since this is a bot)
4. Set environment variables:
   - `TELEGRAM_BOT_TOKEN` = your bot token
   - `TELEGRAM_CHAT_ID` = your chat ID
5. Pick the cheapest plan ($5/mo) and deploy

#### Option C: Docker Compose (Any VPS)

```bash
git clone https://github.com/Pankaj3112/sasta-flight.git
cd sasta-flight
cp .env.example .env
# Edit .env with your bot token and chat ID
docker compose up -d
```

#### Option D: Run Locally

```bash
git clone https://github.com/Pankaj3112/sasta-flight.git
cd sasta-flight
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your bot token and chat ID
python -m bot.main
```

## Usage

Once the bot is running, message it on Telegram:

```
/add ATQ BOM                 Add a one-way route
/add VIX MXP 10              Round-trip with 10-day stay
/add VIX,GIG MXP,BGY 7-10    Multi-airport + flexible stay
/alert 1 target 2800         Alert when cheapest ≤ 2800
/alert 1 drop 8              Alert on ≥8% drop vs last scan
/check                       Scan all routes now
/routes                      List saved routes
/remove 1                    Remove route by ID
/time 07:30                  Change scan start time
/history                     Price trend + median/low
/pause                       Pause scheduled scans
/resume                      Resume scheduled scans
/help                        Show all commands
```

## Daily Message Example

```
✈️ VIX,GIG ⇄ MXP | Next 30 Days | 7-10-day stay
━━━━━━━━━━━━━━━━━━━━━━

🏆 Cheapest: Mar 18 (Tue) → Mar 28 (Fri) - R$4,500 (≈ €720 / $780)
   Airports: GIG → MXP
   TAP | 08:30 PM | 12h 15m | 1 stop

📊 Top 5 Cheapest Days:
 1. Mar 18 (Tue) → Mar 28 (Fri) - R$4,500  [Book →]
 2. Mar 20 (Thu) → Mar 30 (Sat) - R$4,720  [Book →]

📈 Avg: R$5,100 | Low: R$4,500 | High: R$6,200
📉 Hist median: R$5,300 | Hist low: R$4,400

💡 Trend: Prices dropped 8% since last scan
```

EUR/USD amounts are **approximate** conversions (Frankfurter / central-bank rates), not live airline quotes in those currencies.

Split-ticket deals (separate one-way fares cheaper than a round-trip package) are labeled clearly and get two booking links. Separate tickets are riskier (missed connection, baggage, schedule changes).

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | — | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Yes | — | Your Telegram chat ID |
| `DAYS_TO_SCAN` | No | `30` | Days ahead to scan |
| `TOP_CHEAPEST` | No | `5` | How many cheapest days to show |
| `CANDIDATE_POOL` | No | `6` | Calendar candidates to confirm in detail |
| `MAX_CONCURRENT_SEARCHES` | No | `1` | Parallel detail lookups (keep at 1 to avoid 429) |
| `MAX_AIRPORT_COMBOS` | No | `6` | Cap on origin×destination pairs |
| `ENABLE_SPLIT_TICKETS` | No | `0` | Compare OW+OW vs round-trip (extra API calls) |
| `SEARCH_MIN_INTERVAL_SECS` | No | `2.0` | Minimum gap between Google Flights calls |
| `SEARCH_429_MAX_RETRIES` | No | `4` | Retries on HTTP 429 with backoff |
| `SEARCH_429_BASE_DELAY_SECS` | No | `8` | Base delay for 429 exponential backoff |
| `SEARCH_CIRCUIT_THRESHOLD` | No | `3` | Consecutive 429s before circuit opens |
| `SEARCH_CIRCUIT_COOLDOWN_SECS` | No | `300` | Pause scans while circuit is open |
| `CALENDAR_CHUNK_DAYS` | No | `30` | Split long calendars into chunks |
| `CALENDAR_CHUNK_PAUSE_SECS` | No | `3` | Pause between calendar chunks |
| `STAY_SAMPLE_STEP` | No | `1` | Sample every N days in a stay range (use `2` for wide ranges) |
| `TIMEZONE` | No | `Asia/Kolkata` | Timezone for scheduling |
| `CURRENCY` | No | `BRL` | Primary currency (`BRL`, `USD`, `EUR`, `GBP`) |
| `ALWAYS_SEND_SCAN_SUMMARY` | No | `1` | Send full summary every scan (`0` = alerts only) |
| `DEFAULT_ALERT_DROP_PCT` | No | `5` | Default % drop alert threshold |
| `DEFAULT_ALERT_COOLDOWN_MINUTES` | No | `360` | Min minutes between alerts |
| `FARE_PROVIDER` | No | `fli` | Fare provider (`fli` today; extension point) |
| `DB_PATH` | No | `data/flights.db` | SQLite database path |

## How It Works

1. **Calendar pool** — uses [Fli](https://github.com/punitarani/fli) (`SearchDates`) to score candidate dates (and optional airport / stay combinations), chunked for long windows.
2. **Confirm + re-rank** — fetches detailed cheapest flights for the candidate pool (rate-limited), then sorts by **confirmed** price (not calendar estimate).
3. **Rate limiting** — global min-interval between Google calls, exponential backoff on HTTP 429, and a circuit breaker that pauses scans when Google keeps blocking.
4. **Multi-currency display** — quotes are requested in `CURRENCY`; EUR/USD equivalents come from [Frankfurter](https://frankfurter.dev/) with SQLite cache.
5. **Tracking** — each scan writes `scan_runs` + `fare_snapshots` (intraday history) and a daily `price_history` row for charts.
6. **Alerts** — target price, % drop vs last scan, and new historical low, with cooldown + fingerprint dedupe.

There is **no official public Google Flights API**. Fli talks to Google’s unofficial internal endpoints and can break or be rate-limited. The bot wraps fares behind a `FareProvider` interface so a future paid/affiliate provider can be plugged in without rewriting handlers.

## Tech Stack

- Python 3.12
- [Fli](https://github.com/punitarani/fli) — Google Flights data (no API key)
- [Frankfurter](https://frankfurter.dev/) — FX rates (no API key)
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) — Telegram bot
- SQLite — history, FX cache, alerts
- Docker — containerized deployment

## Tests

```bash
pip install -r requirements.txt
pytest
```

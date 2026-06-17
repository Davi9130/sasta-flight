# SastaFlight ✈️

Daily flight price scanner Telegram bot. Scans Google Flights for the cheapest days to fly on your routes and sends you a daily summary.

**What it does:** Every morning (or whenever you choose), you get a Telegram message with the 5 cheapest days to fly in the next 30 days for each of your saved routes — with prices, airlines, and trends.

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
/add ATQ BOM          Add a one-way route (Amritsar → Mumbai)
/add VIX MXP 10       Add round-trip with 10-day stay (Vitória → Milan)
/check                Scan all routes right now
/routes               List your saved routes
/remove 1             Remove route by ID
/time 07:30           Change daily scan time (default: 08:00 IST)
/history              See 7-day price trend
/pause                Pause daily updates
/resume               Resume daily updates
/help                 Show all commands
```

## Daily Message Example

One-way:

```
✈️ ATQ → BOM | Next 30 Days
━━━━━━━━━━━━━━━━━━━━━━

🏆 Cheapest: Mar 18 (Tue) - R$3,200
   IndiGo | 06:00 AM | 2h 45m | Nonstop

📊 Top 5 Cheapest Days:
 1. Mar 18 (Tue) - R$3,200
 2. Mar 20 (Thu) - R$3,450
 3. Mar 25 (Tue) - R$3,500
 4. Mar 12 (Wed) - R$3,800
 5. Mar 15 (Sat) - R$4,100

📈 Avg: R$5,200 | Low: R$3,200 | High: R$8,900

💡 Trend: Prices dropped 8% since yesterday
```

Round-trip (`/add VIX MXP 10`):

```
✈️ VIX ⇄ MXP | Next 30 Days | 10-day stay
━━━━━━━━━━━━━━━━━━━━━━

🏆 Cheapest: Mar 18 (Tue) → Mar 28 (Fri) - R$4,500
   TAP | 08:30 PM | 12h 15m | 1 stop

📊 Top 5 Cheapest Days:
 1. Mar 18 (Tue) → Mar 28 (Fri) - R$4,500  [Book →]
 2. Mar 20 (Thu) → Mar 30 (Sat) - R$4,720  [Book →]

📈 Avg: R$5,100 | Low: R$4,500 | High: R$6,200
```

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | — | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Yes | — | Your Telegram chat ID |
| `DAYS_TO_SCAN` | No | `30` | Number of days ahead to scan |
| `TOP_CHEAPEST` | No | `5` | How many cheapest days to show |
| `TIMEZONE` | No | `Asia/Kolkata` | Timezone for scheduling |
| `CURRENCY` | No | `BRL` | Price currency (`BRL`, `USD`, `EUR`, `GBP`) |
| `DB_PATH` | No | `data/flights.db` | SQLite database path |

## How It Works

- Uses [Fli](https://github.com/punitarani/fli) to query Google Flights' internal API
- Only 2 API calls per route per scan (one for date prices, one for flight details)
- Price history stored in SQLite for trend tracking
- If a scan fails, it retries once after 4 hours

## Tech Stack

- Python 3.12
- [Fli](https://github.com/punitarani/fli) — Google Flights data (no API key needed)
- [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) — Telegram bot framework
- SQLite — price history and config storage
- Docker — containerized deployment

# SastaFlight ✈️

A Telegram bot that scans Google Flights on a schedule. It finds the cheapest days to fly on your saved routes, tracks price history, and alerts you when a route hits your target price.

Fares come from [fast-flights](https://github.com/AWeirdDev/flights). Every Google request goes through a guard that budgets, paces and pauses traffic, so the server's IP doesn't get blocked.

## Quick Start

### 1. Create a Telegram bot

1. Message [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token.
2. Message [@userinfobot](https://t.me/userinfobot) and copy your chat `Id`.

### 2. Run

#### Docker Compose (any VPS)

```bash
cp .env.example .env   # set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
docker compose up -d
```

#### Locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m bot.main
```

Railway or DigitalOcean also work. Deploy it as a worker (not a web service), set the two Telegram variables, and mount a volume at `/app/data` so the database survives redeploys.

### Coming from the Fli-based version?

The new database is `data/sastaflight.db`. To import your routes (IDs are kept), settings and daily price history from the old file, run:

```bash
python -m scripts.import_legacy_db --legacy data/flights.db
```

## Usage

```
/add ATQ BOM                 One-way route
/add VIX MXP 10              Round trip, 10-day stay
/add VIX,GIG MXP,BGY 7-10    Multi-airport + flexible stay
/via VIX MXP 7-10 0-2        Separate tickets via SP/RJ, 0-2 hub nights
/via VIX MXP 10 1 save       Search and monitor that combination
/alert 1 target 2800         Notify only when cheapest ≤ 2800
/alert 1 drop 8              Optional: also alert on ≥8% drop
/alert 1 low on              Optional: alert on a new historical low
/alert 1 clear               Remove alert rules
/check                       Scan all routes now (silent unless an alert fires)
/routes                      List routes (buttons to change stops / frequency)
/stops                       Default stops preference
/frequency                   Default scan frequency (1h … 24h)
/time 07:30                  Daily start time that scan slots align to
/history                     Price trend + median / low
/status                      Google request budget, pause state, proxies, queue
/remove 1                    Remove route
/pause  /resume              Pause or resume scheduled scans
```

Scans are silent by default. History is always saved, and a message goes out only when an alert rule fires, or when scans get postponed by the anti-block guard. Set `ALWAYS_SEND_SCAN_SUMMARY=1` to get the full summary after every scan.

## How it works

### Searching without a calendar API

fast-flights prices one date (or one date pair) per request; there is no "whole month" endpoint. Each scan therefore runs in two phases under a per-scan request budget:

1. **Coarse pass.** It samples the window evenly, by default every `COARSE_STEP_DAYS` days or wider if the budget requires it. For flexible stays it samples the shortest, longest and middle stay. With multiple airports, pairs it has never seen go first, then pairs that were cheapest before.
2. **Refinement.** It takes the `REFINE_TOP_K` cheapest results and checks neighbouring days (±1, then ±2) and neighbouring stay lengths. It walks outward while that keeps finding cheaper fares.

Each answer is already a confirmed price with airline, times, stops and duration. With `ENABLE_SPLIT_TICKETS=1`, the best round trips are also compared against two one-way tickets.

`/via` works the same way, leg by leg. It samples the long-haul legs from the hubs (`VIA_LONGHAUL_HUBS`) first, because they drive the price. Then it prices the return legs for the best outbound dates, and finally the positioning flights to and from São Paulo or Rio, but only around the cheapest long-haul pairs. A same-day self-transfer must leave at least `MIN_SELF_TRANSFER_HOURS` between flights.

### Not getting blocked

- **A single queue.** Only one scan runs at a time, and route jobs are staggered and jittered (±`SCHEDULE_JITTER_MINS`).
- **Pacing.** Each request waits a random `REQ_JITTER_MIN_SECS` to `REQ_JITTER_MAX_SECS` seconds after the previous one.
- **Budgets per scan, per hour and per day.** They're stored in SQLite. When a budget runs out, the scan is postponed and retried automatically, without pushing more requests at Google.
- **Explicit block detection.** HTTP 429/403/503, the `/sorry/` captcha page, consent pages, or a page without flight data all count as a block. Network errors don't.
- **A persistent circuit breaker.** On a direct connection, a block pauses every Google request for `BLOCK_COOLDOWN_SECS`. The pause doubles on each consecutive block, up to `BLOCK_COOLDOWN_MAX_SECS`. The pause survives restarts, and you get one Telegram notice per episode, not one per route.
- **Optional proxies** (`PROXY_URLS`). Requests rotate across proxies. A blocked proxy is quarantined for `PROXY_QUARANTINE_SECS` while the others keep working. Residential proxies are what actually fix repeated blocks.
- **A quote cache** (`QUOTE_CACHE_TTL_MINS`). Overlapping routes, `/check` and `/via` reuse recent answers for free.

Check `/status` at any time to see how much of the budget is used and whether Google is paused.

There is no official Google Flights API. fast-flights reads Google's public results page, so it can break when Google changes that page. Tests include a real captured payload (`tests/fixtures/`) to catch parser changes.

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | — | Bot token from @BotFather (required) |
| `TELEGRAM_CHAT_ID` | — | Your chat ID (required; other chats are ignored) |
| `TIMEZONE` | `America/Sao_Paulo` | Scheduling timezone |
| `CURRENCY` | `BRL` | `BRL`, `USD`, `EUR` or `GBP` (EUR/USD shown as ≈ via Frankfurter) |
| `DAYS_TO_SCAN` | `30` | Outbound window starting tomorrow |
| `TOP_CHEAPEST` | `5` | Days listed per message |
| `MAX_AIRPORT_COMBOS` | `4` | Cap on origin × destination pairs |
| `ENABLE_SPLIT_TICKETS` | `0` | Compare round trip vs two one-ways (2 extra requests per candidate) |
| `ALWAYS_SEND_SCAN_SUMMARY` | `0` | `1` = message after every scan |
| `DEFAULT_ALERT_COOLDOWN_MINUTES` | `360` | Minimum gap between alerts for a route |
| `PROXY_URLS` | empty | Comma-separated proxies (`http://user:pass@host:port`, `socks5://…`) |
| `REQ_BUDGET_PER_SCAN` | `25` (`40` with proxies) | Google requests one route scan may use |
| `REQ_BUDGET_PER_HOUR` | `60` (`200`) | Global hourly cap |
| `REQ_BUDGET_PER_DAY` | `400` (`2000`) | Global daily cap |
| `REQ_JITTER_MIN_SECS` / `REQ_JITTER_MAX_SECS` | `4` / `9` (`2` / `5`) | Random gap between requests |
| `BLOCK_COOLDOWN_SECS` | `1800` | First pause after a block (doubles per consecutive block) |
| `BLOCK_COOLDOWN_MAX_SECS` | `21600` | Longest pause |
| `PROXY_QUARANTINE_SECS` | `1800` | How long a blocked proxy sits out |
| `QUOTE_CACHE_TTL_MINS` | `180` | Reuse answers for this long |
| `SCHEDULE_JITTER_MINS` | `10` | Random delay added to each scheduled scan |
| `COARSE_STEP_DAYS` | `3` | Minimum spacing of the coarse pass |
| `REFINE_TOP_K` / `REFINE_RADIUS_DAYS` | `3` / `2` | Refinement breadth / depth |
| `VIA_BUDGET_PER_SCAN` | `40` (`60`) | Request budget for one `/via` search |
| `VIA_LONGHAUL_HUBS` | `GRU,GIG` | Long-haul airports considered for `/via` |
| `MIN_SELF_TRANSFER_HOURS` | `4` | Minimum same-day self-transfer gap |
| `DB_PATH` | `data/sastaflight.db` | SQLite database |

A rough sizing rule: a round trip with a 7-10 day stay over 30 days uses about 25 requests per scan. Without proxies, the defaults allow roughly two such scans per hour, so keep the scan frequency at 6h or slower when you track several routes.

## Project layout

```
bot/
  fares/       client.py (fast-flights + block detection), guard.py, cache.py, service.py
  search/      planner.py (date sampling), scan.py (routes, split tickets), hubs.py (/via)
  runner.py    scan → persist → alerts → Telegram
  scheduler.py per-route jobs feeding one serial queue
  handlers.py  Telegram commands
  db.py  formatter.py  alerts.py  fx.py  config.py  main.py
scripts/import_legacy_db.py
```

## Tests

```bash
pip install -r requirements.txt
pytest
```

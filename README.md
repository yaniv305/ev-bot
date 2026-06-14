# EV Betting Bot

## Overview

A Python bot that finds positive expected value (+EV) betting opportunities in real time. It scrapes live odds from Winner.co.il, compares them against Pinnacle's sharp market odds, and sends Telegram alerts whenever the expected value exceeds 4%.

Pinnacle is widely considered the sharpest bookmaker in the world — their odds are used as a proxy for the true probability of an outcome. When Winner offers higher odds than Pinnacle's implied probability suggests, a +EV opportunity exists.

## How It Works

```
agents/coverage_scan_agent.py    (runs once daily at 08:00 IL)
      │
      │  Scrapes all Winner markets via Playwright
      │  Matches Winner games → Pinnacle IDs using Claude Haiku
      │  Writes data/coverage_{date}.json  (sport_key, pinnacle_id, kickoff)
      ▼

match_odds.py    (run manually when games are in window)
      │
      │  Loads today's coverage pairs
      │  Filters to soccer games within kickoff −60 / +90 min window
      │  Fetches Winner live odds via WinnerScraper
      │  Calls get_event_odds() per game → h2h + totals + BTTS
      ▼
ev_calculator.py
      │
      │  EV = (true_probability × winner_odds) − 1
      │  true_prob derived by devigging Pinnacle overround
      │  Alerts when EV ≥ 4% across 1X2 / Over-Under / BTTS markets
      ▼
telegram_bot.py
      │
      │  Sends formatted alert to Telegram channel
      ▼
   📲 Alert
```

## Soccer Market Scope (Phase 1)

| Market | Pinnacle source | Notes |
|---|---|---|
| 1X2 full-time | `h2h` (3-way) | Home / Draw / Away, all three required |
| Over/Under goals | `totals + alternate_totals` merged | Exact line match; all lines 1.25→3.5 available |
| BTTS | `btts` | Yes / No |

Basketball, Baseball, Tennis: deferred to Phase 2.

## Technical Highlights

**Bypassed Imperva bot protection** — Winner.co.il is protected by Imperva Bot Manager. The bot uses a warm Playwright browser session with `playwright-stealth` to pass the JavaScript challenge, then intercepts the internal `GetCMobileLine` API response directly.

**Zero-cost daily coverage scan** — The coverage agent uses only free Odds API endpoints (`/sports` and `/sports/{key}/events`) to build a complete Winner↔Pinnacle match map each morning. The expensive per-game call (`/events/{id}/odds`) is deferred until a game enters the active window.

**Claude Haiku for game matching** — Winner displays team names in Hebrew; Pinnacle uses English. The coverage agent sends Hebrew descriptions and English Pinnacle candidates to Haiku, which matches by phonetics and context. Results are stored in `data/coverage_{date}.json` and reused for the rest of the day.

**Per-game event endpoint** — The Odds API's event-specific endpoint (`/v4/sports/{key}/events/{id}/odds`) supports `alternate_totals` and `btts` markets that the batch `/upcoming` endpoint does not. This gives access to all available totals lines (not just the main line) at 4 quota per game call.

**Exact line matching** — Winner offers one fixed totals line per game. The bot extracts the point value and looks for an exact match in Pinnacle's merged `totals + alternate_totals` list. No rounding, no closest-line fallback — if Pinnacle doesn't offer that line, the market is skipped.

**Atomic translations cache** — `translations.json` stores bootstrapped sport IDs. All writes go to a `.tmp` file first, then `rename()` atomically to prevent corruption on crash.

## Tech Stack

| Tool | Purpose |
|---|---|
| Python 3.12 | Core language |
| Playwright + playwright-stealth | Headless browser, bot protection bypass |
| Claude API (Haiku) | Hebrew→English game matching in coverage scan |
| The Odds API | Pinnacle sharp odds |
| SQLite | Alert storage and deduplication |
| python-telegram-bot | Telegram alert delivery |

## Project Structure

**Pipeline:**
```
agents/coverage_scan_agent.py   — Daily scan (08:00 IL): matches Winner↔Pinnacle across all sports
match_odds.py                   — Manual EV scan: runs against today's coverage pairs
winner_scraper.py               — Playwright scraper: intercepts Winner's internal API
pinnacle_client.py              — The Odds API client: get_event_odds() per game
ev_calculator.py                — EV calculation for 1X2, Over/Under, BTTS markets
telegram_bot.py                 — Sends +EV alerts + daily coverage report to Telegram
database.py                     — SQLite: alert deduplication and storage
```

**Data & Storage:**
```
translations.json               — winner_sport_ids bootstrap cache
alerts.db                       — SQLite alert history
data/coverage_{date}.json       — Matched Winner↔Pinnacle pairs (gitignored)
data/orphans_{date}.json        — Unmatched games debug report (gitignored)
```

**Tests:**
```
tests/test_ev_calculator.py     — pytest tests for EV calculation logic
```

**Config:**
```
.env.example        — Required environment variables template
requirements.txt    — Python dependencies
```

## Setup

```bash
git clone https://github.com/yaniv305/ev-bot.git
cd ev-bot
pip install -r requirements.txt
playwright install chromium
cp .env.example .env   # then fill in your API keys

# Run coverage scan first (or wait for 08:00 IL)
python agents/coverage_scan_agent.py

# Then run EV scan when games are in window
python match_odds.py
```

Requires Python 3.12+.

## Environment Variables

```
ANTHROPIC_API_KEY=...
ODDS_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

## What I Learned

Building this bot forced me to solve real engineering problems rather than textbook ones. Bypassing Imperva's bot protection meant understanding how browsers actually behave at the network level, not just how to write a scraper. Integrating three external APIs (Playwright, Claude, The Odds API) taught me how to handle rate limits, cache data intelligently, and design systems that degrade gracefully when one component fails.

Working in Hebrew added an unexpected challenge — the mismatch between Winner's Hebrew team names and Pinnacle's English names had no clean solution, so I built one using Claude Haiku with a daily coverage scan that matches games phonetically. The coverage scan runs once in the morning and the results are reused all day, keeping live API costs low while still catching every +EV opportunity in the window.

# EV Betting Bot

## Overview

A Python bot that finds positive expected value (+EV) betting opportunities in real time. It scrapes live odds from Winner.co.il, compares them against Pinnacle's sharp market odds, and sends Telegram alerts whenever the expected value exceeds 4%.

Pinnacle is widely considered the sharpest bookmaker in the world — their odds are used as a proxy for the true probability of an outcome. When Winner offers higher odds than Pinnacle's implied probability suggests, a +EV opportunity exists.

## How It Works

**Current production pipeline (master):**

```
winner_scraper.py
      │
      │  Live football odds (Hebrew) via Playwright browser interception
      ▼
matcher.py  +  pinnacle_client.py
      │
      │  Hebrew→English team translation via Claude AI + translations.json cache
      │  Matches by league mapping + kickoff proximity
      ▼
ev_calculator.py  →  telegram_bot.py  →  📲 Alert
```

**New matching service (this branch — not yet wired in):**

```
winner_scraper.py
      │
      │  Live odds — all sports
      ▼
matching_service.py  +  vector_db.py
      │
      │  Daily index of all Pinnacle events (free /events endpoint)
      │  Time-window filter (±30 min) + Claude Haiku confirmation
      │  No manual league mappings — works across all sports
      ▼
pinnacle_client.py  →  ev_calculator.py  →  telegram_bot.py  →  📲 Alert
```

The full pipeline runs every 20 minutes between 12:00–22:00 Israel time. A daily prep agent runs at 11:50 IL.

## Technical Highlights

**Bypassed Imperva bot protection** — Winner.co.il is protected by Imperva Bot Manager. Rather than scraping HTML, the bot uses a warm Playwright browser session with `playwright-stealth` to pass the JavaScript challenge, then intercepts the internal `GetCMobileLine` API response directly. The browser session is kept alive between runs (reload, not relaunch) for stability and speed.

**All-sports matching via time-window + LLM** — Winner displays team names in Hebrew across all sports; Pinnacle uses English. The matching pipeline builds a daily index of every Pinnacle event (480+ events across 30+ leagues), then for each Winner game finds all Pinnacle events starting within ±30 minutes and asks Claude Haiku to confirm whether any of them is the same game. This approach handles all sports without any manual league mapping or team name dictionary, and routes every candidate through the LLM — even when only one exists — to eliminate false positives.

**Smart Pinnacle cache with key-set invalidation** — Pinnacle odds are cached for 20 minutes to reduce API usage. The cache is invalidated not just on TTL expiry but also whenever new sport keys appear in the Winner window that weren't included in the previous fetch, preventing stale-cache false negatives.

**Results tracking** — Every sent alert is stored in SQLite. After each game kicks off, the bot polls The Odds API scores endpoint to fetch the final result and records it against the original alert for performance tracking.

**Clean modular architecture** — each file has a single responsibility. The scraper, matching service, odds client, calculator, and notifier are fully independent and can be run in isolation.

## Tech Stack

| Tool | Purpose |
|---|---|
| Python 3.12 | Core language |
| Playwright + playwright-stealth | Headless browser, bot protection bypass |
| Claude API (Haiku) | Cross-lingual game matching (Hebrew → English) |
| The Odds API | Pinnacle sharp odds + match results |
| SQLite | Alert storage and deduplication |
| python-telegram-bot | Telegram alert delivery |

## Project Structure

**Pipeline** (run in order by `scheduler.py`):
```
scheduler.py          — Main loop: runs every 20 min between 12:00–22:00 IL
winner_scraper.py     — Scrapes live odds from Winner.co.il via Playwright
pinnacle_client.py    — Fetches Pinnacle events and sharp odds via The Odds API
matcher.py            — Matches Winner games to Pinnacle (current, football-only)
ev_calculator.py      — Calculates Expected Value, filters opportunities above 4%
telegram_bot.py       — Sends +EV alerts to Telegram
```

**New matching service (in development — not yet integrated):**
```
matching_service.py   — All-sports matcher: time-window + Claude AI confirmation
vector_db.py          — Daily index of Pinnacle events, persisted to data/
test_matching.py      — Standalone runner: builds DB, matches, saves data/matched_games.json
```

**Agents:**
```
agents/daily_prep_agent.py  — Builds the daily Pinnacle event index at 11:50 IL
```

**Data & Storage:**
```
translations.json   — League name mappings + team name translation cache
database.py         — SQLite tracking for alerts and match results
results_fetcher.py  — Fetches final scores via The Odds API scores endpoint
```

**Tests:**
```
tests/test_ev_calculator.py — pytest unit tests for EV calculation logic
test_matching.py            — Standalone runner: builds DB, runs matching, saves data/matched_games.json
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
python scheduler.py
```

Requires Python 3.12+. The bot runs every 20 minutes between 12:00–22:00 Israel time.

## Environment Variables

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API — used for game matching |
| `ODDS_API_KEY` | The Odds API — Pinnacle events and odds |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Telegram channel/chat to send alerts to |

## What I Learned

Building this bot forced me to solve real engineering problems rather than textbook ones. Bypassing Imperva's bot protection meant understanding how browsers actually behave at the network level, not just how to write a scraper. Integrating three external APIs (Playwright, Claude, The Odds API) taught me how to handle rate limits, cache data intelligently, and design systems that degrade gracefully when one component fails.

The matching problem turned out to be the most interesting part. The first approach — manual league mappings plus per-team Claude translation — worked for football but didn't scale to other sports. The second approach uses the free Pinnacle `/events` endpoint to build a daily index of everything Pinnacle offers, then matches by kickoff time and confirms with the LLM. It handles all sports without any configuration, and because the LLM sees the actual Hebrew and English names, false positives are reliably caught.

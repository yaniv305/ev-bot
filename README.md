# EV Betting Bot

## Overview

A Python bot that finds positive expected value (+EV) betting opportunities in real time. It scrapes live odds from Winner.co.il, compares them against Pinnacle's sharp market odds, and sends Telegram alerts whenever the expected value exceeds 4%.

Pinnacle is widely considered the sharpest bookmaker in the world — their odds are used as a proxy for the true probability of an outcome. When Winner offers higher odds than Pinnacle's implied probability suggests, a +EV opportunity exists.

## How It Works

```
winner_scraper.py
      │
      │  Live 1X2 odds (Hebrew) via Playwright browser interception
      ▼
  matcher.py
      │
      │  Hebrew→English team name translation via Claude AI
      │  Matches Winner games to Pinnacle games by league + kickoff time
      ▼
pinnacle_client.py
      │
      │  True probabilities via The Odds API (Pinnacle sharp odds)
      ▼
ev_calculator.py
      │
      │  EV = (true_probability × winner_odds) − 1
      │  Alerts when EV ≥ 4%
      ▼
telegram_bot.py
      │
      │  Sends formatted alert to Telegram channel
      ▼
   📲 Alert
```

The full pipeline is scheduled every 10 minutes by `scheduler.py`, active between 12:00–22:00 Israel time.

## Technical Highlights

**Bypassed Imperva bot protection** — Winner.co.il is protected by Imperva Bot Manager. Rather than scraping HTML, the bot uses a warm Playwright browser session with `playwright-stealth` to pass the JavaScript challenge, then intercepts the internal `GetCMobileLine` API response directly. This approach mimics real browser behavior and has proven stable.

**Claude AI for team name translation** — Winner displays team names in Hebrew; Pinnacle uses English. Rather than maintaining a full manual dictionary, the bot passes the Hebrew name and the list of Pinnacle team names for that league to Claude Haiku, which picks the correct match. Translations are cached persistently in `translations.json`. If a cached translation causes a match failure, the bot automatically deletes the bad entry and retries with a fresh Claude call, logging `[RETRANSLATED]` on correction.

**Smart Pinnacle cache with key-set invalidation** — Pinnacle odds are cached for 20 minutes to reduce API usage. The cache is invalidated not just on TTL expiry but also whenever new sport keys appear in the Winner window that weren't included in the previous fetch, preventing `[NO PINNACLE DATA]` false negatives.

**Structured match diagnostics** — Every unmatched Winner game is logged with an explicit reason: `[SKIPPED]` (league not mapped), `[NO PINNACLE DATA]` (mapped league but empty Pinnacle window), `[NO MATCH]` (translation failed), or `[NO MATCH - CONFIRMED]` (retry also failed). Skipped leagues are summarised with game counts at the end of each run.

**Clean modular architecture** — each file has a single responsibility. The scraper, odds client, matcher, calculator, and notifier are fully independent and can be tested in isolation.

## Tech Stack

| Tool | Purpose |
|---|---|
| Python 3.12 | Core language |
| Playwright + playwright-stealth | Headless browser, bot protection bypass |
| Claude API (Haiku) | Hebrew→English team name translation |
| The Odds API | Pinnacle sharp odds |
| SQLite | Alert storage and deduplication |
| python-telegram-bot | Telegram alert delivery |
| Git | Version control |

## Project Structure

**Pipeline** (run in order by `scheduler.py`):
```
scheduler.py        — Main loop: runs every 10 min between 12:00–22:00 IL
winner_scraper.py   — Scrapes live football odds from Winner.co.il via Playwright
pinnacle_client.py  — Fetches sharp odds from Pinnacle via The Odds API
matcher.py          — Matches Winner games against Pinnacle using Claude AI translation
ev_calculator.py    — Calculates Expected Value, filters opportunities above 4%
telegram_bot.py     — Sends +EV alerts to Telegram
```

**Agents:**
```
agents/daily_prep_agent.py  — Daily prep agent: verifies leagues, team translations, and measures
                              today's match coverage before the 12:00 run (runs at 11:55 IL)
```

**Data & Storage:**
```
translations.json   — League name mappings + team name translation cache
database.py         — SQLite tracking for alerts and match results
results_fetcher.py  — Scrapes Winner results page to fill in final scores
```

**Tests:**
```
tests/test_ev_calculator.py — pytest tests for EV calculation logic
```

**Config:**
```
.env.example        — Required environment variables template
requirements.txt    — Python dependencies
PLAYBOOK.md         — Matching conditions and system logic documentation
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

Requires Python 3.12+. The bot runs every 10 minutes between 12:00–22:00 Israel time.

## What I Learned

Building this bot forced me to solve real engineering problems rather than textbook ones. Bypassing Imperva's bot protection meant understanding how browsers actually behave at the network level, not just how to write a scraper. Integrating three external APIs (Playwright, Claude, The Odds API) taught me how to handle rate limits, cache data intelligently, and design systems that degrade gracefully when one component fails.

Working in Hebrew added an unexpected challenge — the mismatch between Winner's Hebrew team names and Pinnacle's English names had no clean solution, so I built one using Claude AI with persistent caching and automatic self-correction. That loop of building, running, finding edge cases in live data, and fixing them is what I think real software development actually looks like.

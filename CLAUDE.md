# EV Betting Bot

## Project Overview
Automated system that detects +EV betting opportunities by comparing Winner.co.il odds against Pinnacle sharp odds in real time. Sends Telegram alerts when EV exceeds 4%.

## Architecture

### Production pipeline (master)
- `scheduler.py` — main loop, runs every 20 min between 12:00–22:00 IL
- `winner_scraper.py` — scrapes Winner.co.il via persistent Playwright browser session
- `pinnacle_client.py` — fetches Pinnacle odds via The Odds API
- `matcher.py` — matches Winner games to Pinnacle using Claude API translation (soccer only)
- `ev_calculator.py` — calculates Expected Value, filters above 4% threshold
- `telegram_bot.py` — sends alerts to Telegram
- `database.py` — SQLite tracking for alerts and results
- `results_fetcher.py` — fetches match results via The Odds API scores endpoint
- `translations.json` — league mappings + team name translation cache
- `agents/daily_prep_agent.py` — daily AI agent, runs at 11:50 IL

### New matching service (feature/matching-service — not yet integrated)
- `matching_service.py` — all-sports matcher: time-window (±30 min) + Claude Haiku confirmation
- `vector_db.py` — daily index of Pinnacle events, persisted to `data/vector_db/{date}.json`
- `test_matching.py` — standalone runner: builds DB, runs matching, saves `data/matched_games.json`

The new matching service replaces `matcher.py` + `daily_prep_agent.py` once integrated.
It covers all sports (Soccer, Baseball, Basketball, Tennis) without manual league mappings.

## Key Technical Decisions
- Playwright keeps browser session alive between runs (reload not relaunch)
- Claude API (Haiku) handles Hebrew→English game matching via LLM disambiguation
- Every candidate routed through LLM — even single candidates — to prevent false positives
- Pinnacle odds cached for 20 minutes to reduce API quota usage
- Run interval: 20 minutes
- Winner sport_id → Pinnacle group: 226=Baseball, 227=Basketball, 239=Tennis, 240/1100=Soccer

## Environment Variables (.env)
- ANTHROPIC_API_KEY
- ODDS_API_KEY
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID

## Testing
- `pytest tests/` — runs unit tests (CI on every push)
- `python test_matching.py` — manual live test for the matching service

## Branch Convention
- master — production
- feature/ — new features
- qa/ — testing work
- optimize/ — performance improvements

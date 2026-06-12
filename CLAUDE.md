# CLAUDE.md — EV Bot

## What This Project Does

Finds +EV betting opportunities by comparing Winner.co.il live odds against Pinnacle's sharp odds. When Winner offers higher odds than Pinnacle's implied probability, an alert is sent to Telegram.

---

## Architecture

```
winner_scraper.py       Playwright browser scraper — intercepts Winner's internal API
matcher.py              Matches Winner games to Pinnacle by league + kickoff + Claude translation
pinnacle_client.py      Fetches Pinnacle odds via The Odds API (/v4/sports/{key}/odds)
ev_calculator.py        EV = (pinnacle_probability × winner_odds) − 1 — alerts when ≥ 4%
telegram_bot.py         Sends formatted alerts to Telegram channel
database.py             SQLite — deduplicates alerts, stores results
results_fetcher.py      Scrapes Winner results to fill in final scores
scheduler.py            Main loop — orchestrates everything
```

**Agents:**
```
agents/daily_prep_agent.py      Runs at 11:50 IL — verifies leagues, translations, coverage
agents/coverage_scan_agent.py   Runs at 08:00 IL — daily multi-sport Winner↔Pinnacle scan
```

**Shared data:**
```
translations.json   Team name cache + league maps + winner_sport_ids
alerts.db           SQLite database
data/               coverage_{date}.json — matched pairs (gitignored)
                    orphans_{date}.json  — debug report (gitignored)
```

---

## Scheduler Timing

| Time (IL) | Task |
|---|---|
| 08:00 | `run_coverage_scan()` — multi-sport coverage scan (not yet wired) |
| 11:50–11:59 | `run_agent()` — daily prep agent |
| 12:00–22:00 | Pipeline runs every 20 min |

---

## The Odds API (api.the-odds-api.com/v4)

| Endpoint | Cost | Used for |
|---|---|---|
| `GET /v4/sports` | Free | List all active sport keys |
| `GET /v4/sports/{key}/events` | **Free** | Get events: id, teams, kickoff |
| `GET /v4/sports/{key}/odds` | 1 quota/region/market | Live odds (pipeline) |
| `GET /v4/sports/{key}/events/{id}/odds` | 1 quota/region/market | Odds for one event |

The `id` field from `/events` is the stable Pinnacle identifier — used in `coverage_scan_agent.py` output and for future targeted odds lookup.

---

## Key Patterns

**Winner scraper**: uses Playwright with `playwright-stealth` to pass Imperva bot protection. Intercepts the internal `GetCMobileLine` API response. `sport_ids=None` fetches all sports. The scraper is kept alive as a persistent session in the scheduler.

**Team name translation**: Hebrew names passed with Pinnacle candidates to Claude Haiku. Result cached in `translations.json["team_name_cache"]`. Bad translations are auto-deleted and retried on match failure (`[RETRANSLATED]` log).

**`translations.json` write pattern**: always write to a `.tmp` file then `rename()` atomically. Never write directly. Used in `matcher.py`, `daily_prep_agent.py`, and `coverage_scan_agent.py`.

**Sport agent pattern**: sync Claude Haiku agent loop (`_run_sport_agent_sync`) wrapped in `asyncio.to_thread()` for parallel execution without blocking the async event loop.

**Winner event_id deduplication**: Winner returns multiple markets per event (1X2, totals, handicap). Always deduplicate by `event_id` before calling Haiku for game matching.

**Haiku ID extraction**: Haiku sometimes returns the correct hex ID followed by explanation text. Always extract with `re.search(r'\b([0-9a-f]{32})\b', raw)` rather than taking the raw response. If no hex found, check for `"NO_MATCH"` in the text.

**Outright filter (`_is_game`)**: Soccer-only filter applied before `_match_games_in_league`. A description is a real game if it contains ` - ` and neither side contains a 4-digit year. Catches specials like "הזוכה במונדיאל 2026" without dropping actual match descriptions like "ארגנטינה - צרפת".

**Orphan report**: `_write_orphans()` writes `data/orphans_{date}.json` after every scan. `orphan_leagues` = leagues skipped by the agent (no Pinnacle key found). `orphan_games` = games from matched leagues that still had no Pinnacle match, with `reason` and `candidates` fields. Never list individual games from orphan leagues.

---

## Next Session

Build a Telegram report that sends the orphan debug summary: unmatched games, their Pinnacle candidates, and Haiku's reasoning for NO_MATCH. Triggered after the coverage scan completes.

---

## Important Constraints

**Do NOT use `WINNER_LEAGUE_MAP` in `coverage_scan_agent.py`** or any new multi-sport work. Coverage scan discovers leagues dynamically — the whole point is no pre-built map. League matching is done by the sport agent's own reasoning against Pinnacle sport key titles and sample teams.

**Soccer sport_id = 240** is hardcoded. Basketball=227, Tennis=239, Baseball=226 are discovered at bootstrap and stored in `translations.json["winner_sport_ids"]`.

**`matcher.py` is soccer-only** and uses `WINNER_LEAGUE_MAP`. It is used by the 12:00–22:00 pipeline and should not be changed to handle other sports — that's the coverage scan agent's job.

---

## Running Things

```bash
# Full pipeline
python scheduler.py

# Coverage scan only (test)
python agents/coverage_scan_agent.py

# Daily prep agent only (test)
python agents/daily_prep_agent.py
```

---

## Environment Variables (`.env`)

```
ANTHROPIC_API_KEY=...
ODDS_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

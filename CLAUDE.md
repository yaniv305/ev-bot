# CLAUDE.md — EV Bot

## What This Project Does

Finds +EV betting opportunities by comparing Winner.co.il live odds against Pinnacle's sharp odds. When Winner offers higher odds than Pinnacle's implied probability, an alert is sent to Telegram.

---

## Architecture

```
winner_scraper.py       Playwright browser scraper — intercepts Winner's internal API
pinnacle_client.py      Fetches Pinnacle odds via The Odds API (/v4/sports/{key}/events/{id}/odds)
ev_calculator.py        EV = (pinnacle_probability × winner_odds) − 1 — alerts when ≥ 4%
telegram_bot.py         Sends formatted alerts to Telegram channel
database.py             SQLite — deduplicates alerts, stores results
match_odds.py           Manual EV scanner — runs against today's coverage pairs
```

**Agents:**
```
agents/coverage_scan_agent.py   Runs at 08:00 IL — daily multi-sport Winner↔Pinnacle scan
```

**Shared data:**
```
translations.json   winner_sport_ids cache (bootstrap: basketball=227, tennis=239, baseball=226)
alerts.db           SQLite database
data/               coverage_{date}.json — matched pairs with sport_key (gitignored)
                    orphans_{date}.json  — debug report (gitignored)
```

---

## How It Works

1. **Coverage scan** (`agents/coverage_scan_agent.py`) runs at 08:00 IL. Produces `data/coverage_{date}.json` — flat list of pre-matched Winner↔Pinnacle pairs with `sport_key`.

2. **Match odds** (`match_odds.py`) is run manually (or by the scheduler). For each active soccer game in the time window (−60 to +90 min around kickoff):
   - Fetches Winner markets via scraper
   - Calls `get_event_odds(sport_key, pinnacle_id)` → h2h + totals + BTTS in one API call
   - Builds market pairs and runs EV calculation
   - Sends alerts for EV ≥ 4%

---

## Soccer Market Scope (Phase 1)

| Market | Winner filter | Pinnacle market |
|---|---|---|
| 1X2 full-time | `"1X2" in mt AND "תוצאת סיום" in mt` | `h2h` (3-way, draw required) |
| Over/Under goals | `"מעל/מתחת שערים" in mt AND "תוצאת סיום" in mt` | `totals + alternate_totals` merged, exact line match |
| BTTS | `"האם כל קבוצה תבקיע" in mt AND "תוצאת סיום" in mt` | `btts` |

Basketball / Baseball / Tennis: deferred to Phase 2.

---

## The Odds API (api.the-odds-api.com/v4)

| Endpoint | Cost | Used for |
|---|---|---|
| `GET /v4/sports` | Free | List all active sport keys (coverage scan) |
| `GET /v4/sports/{key}/events` | **Free** | Get events: id, teams, kickoff (coverage scan) |
| `GET /v4/sports/{key}/events/{id}/odds` | 1 quota/region/market | Per-game odds fetch in match_odds.py |

Current call: `markets=h2h,totals,alternate_totals,btts` = **4 quota per game**.

The `id` field from `/events` is the stable Pinnacle identifier stored as `pinnacle_id` in coverage pairs.

---

## Key Patterns

**Winner scraper**: uses Playwright with `playwright-stealth` to pass Imperva bot protection. Intercepts the internal `GetCMobileLine` API response. `sport_ids=None` fetches all sports.

**Coverage scan output**: `data/coverage_{date}.json` — each pair has `winner_event_id`, `pinnacle_id`, `sport_key`, `kickoff`, `winner_description`, `pinnacle_name`. The `sport_key` is required by `get_event_odds()`.

**`get_event_odds(sport_key, event_id)`**: fetches h2h + totals + alternate_totals + btts for one event. Merges `totals` and `alternate_totals` into a deduplicated list sorted by point value. Returns `None` if Pinnacle not in response.

**EV formula**: `overround = sum(1/odds for each outcome)` → `true_prob = (1/outcome_odds) / overround` → `EV = true_prob × winner_odds − 1`. Alert when `EV ≥ 0.04`.

**1X2 devig**: requires all three odds (home, draw, away) — `if not all([h, d, a]): continue`. Draw is always present for soccer; removing this guard would produce wrong overround.

**Totals line matching**: Winner offers one fixed line (e.g. "מעל 2.5"). Extract point with `float(desc.split()[-1])`. Find matching Pinnacle line with `next((t for t in pinn_event["totals"] if t["point"] == winner_point), None)`. No rounding, no closest-line fallback.

**BTTS outcome mapping**: Winner `"כן"` → Pinnacle `"Yes"`, Winner `"לא"` → Pinnacle `"No"`.

**`translations.json` write pattern**: always write to a `.tmp` file then `rename()` atomically. Never write directly. Used in `coverage_scan_agent.py`.

**Sport agent pattern**: sync Claude Haiku agent loop (`_run_sport_agent_sync`) wrapped in `asyncio.to_thread()` for parallel execution without blocking the async event loop.

**Winner event_id deduplication**: Winner returns multiple markets per event (1X2, totals, handicap). Always deduplicate by `event_id` before calling Haiku for game matching.

**Haiku ID extraction**: Haiku sometimes returns the correct hex ID followed by explanation text. Always extract with `re.search(r'\b([0-9a-f]{32})\b', raw)` rather than taking the raw response. If no hex found, check for `"NO_MATCH"` in the text.

**Outright filter (`_is_game`)**: Soccer-only filter applied before `_match_games_in_league`. A description is a real game if it contains ` - ` and neither side contains a 4-digit year.

**Orphan report**: `_write_orphans()` writes `data/orphans_{date}.json` after every scan. `orphan_leagues` = leagues skipped by the agent. `orphan_games` = games from matched leagues that had no Pinnacle match, with `reason`, `pinnacle_league`, and `candidates` fields.

**Wrong-key retry**: When `match_games_in_league` returns `warning=all_no_candidates`, the sport agent retries with a different Pinnacle key. Tracked via `already_retried: set[str]`.

**Single-team fallback (`_match_single_team`)**: When Haiku returns NO_MATCH for a full Hebrew description, retries with just the home team name, then just the away team name.

**Telegram coverage report**: `send_coverage_report(total_matched, orphan_report)` — sent after each scan. Shows matched/unmatched per sport, all matched pairs, all orphan games.

---

## Next Session

Build new `scheduler.py` — runs `match_odds.py` logic on a timed interval with `run_coverage_scan()` at 08:00 IL.

---

## Important Constraints

**Do NOT use `WINNER_LEAGUE_MAP`** — it no longer exists. Coverage scan discovers leagues dynamically.

**Soccer sport_id = 240** is hardcoded. Basketball=227, Tennis=239, Baseball=226 are discovered at bootstrap and stored in `translations.json["winner_sport_ids"]`.

**`match_odds.py` is soccer-only (Phase 1).** Non-soccer pairs from coverage are skipped via `sport_key.startswith("soccer")`.

---

## Running Things

```bash
# Coverage scan (run once daily at 08:00 IL, or manually)
python agents/coverage_scan_agent.py

# EV scan (run manually when games are in window)
python match_odds.py
```

---

## Environment Variables (`.env`)

```
ANTHROPIC_API_KEY=...
ODDS_API_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

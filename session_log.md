# Session Log

---

## 2026-06-14 — Soccer EV Pipeline + Project Cleanup

### Branch: feature/coverage-scan-agent

### What Was Built

**`match_odds.py`** — new manual EV scanner, replacing the old `scheduler.py` pipeline:
- Loads `data/coverage_{today}.json` (pre-matched pairs from coverage scan)
- Filters to soccer pairs in the kickoff window (−60 min to +90 min)
- Fetches Winner markets once via `WinnerScraper.get_all_markets()`
- Per active game: calls `get_event_odds()` → builds 3 market pairs (1X2, O/U, BTTS)
- Deduplicates against DB before sending Telegram alerts

**`pinnacle_client.get_event_odds(sport_key, event_id)`** — new function:
- Calls `GET /v4/sports/{sport_key}/events/{event_id}/odds?markets=h2h,totals,alternate_totals,btts`
- Merges `totals` + `alternate_totals` into one deduplicated list keyed by point (gives all lines 1.25→3.5)
- Returns `{home_odds, draw_odds, away_odds, totals: [...], btts_yes_odds, btts_no_odds}`
- Cost: 4 quota per game call

**`ev_calculator.py`** — extended to handle 3 market types:
- `h2h` (1X2): unchanged — `if not all([h,d,a]): continue` kept (draw required for soccer)
- `totals`: 2-way devig; exact line match from `pinn_event["totals"]` list; `מעל`→Over / `מתחת`→Under
- `btts`: 2-way devig; `כן`→Yes / `לא`→No
- All alert dicts now include `winner_event_id` for DB deduplication

**`coverage_scan_agent.py`** — added `sport_key` to each output pair:
- One-liner: `p["sport_key"] = pinnacle_sport_key` after `_match_games_in_league`
- Needed by `match_odds.py` to call the event-specific Pinnacle endpoint

### Soccer Market Scope Confirmed
| Market | Winner keyword | Pinnacle |
|---|---|---|
| 1X2 | `"1X2" in mt AND "תוצאת סיום" in mt` | `h2h` |
| Over/Under goals | `"מעל/מתחת שערים" in mt AND "תוצאת סיום" in mt` | `totals + alternate_totals` (exact line) |
| BTTS | `"האם כל קבוצה תבקיע" in mt AND "תוצאת סיום" in mt` | `btts` |

### Files Deleted (no longer relevant)
- `scheduler.py` — replaced by `match_odds.py` + future scheduler (next session)
- `matcher.py` — replaced by coverage scan + match_odds.py lookup
- `agents/daily_prep_agent.py` — no longer needed
- `results_fetcher.py` — was only used by old scheduler
- `PLAYBOOK.md` — superseded by CLAUDE.md

### Cleaned Up
- `pinnacle_client.py`: removed `get_pinnacle_odds()`, `WINNER_LEAGUE_MAP`, `_load_league_map()`
- `=3.0`: accidental pip output file, deleted

### Test Results
- Coverage scan: 16 pairs with `sport_key` (10 soccer, 3 basketball, 3 baseball)
- `match_odds.py` on Netherlands vs Japan: 3 pairs built (h2h + totals + BTTS), 0 alerts (no +EV at that moment)
- 4 quota used per game call

### Next Session
Build new `scheduler.py` — runs `match_odds.py` logic on a timed interval with `run_coverage_scan()` at 08:00 IL.

---

## 2026-06-13 (continued) — Matched-Games Debug Report + PROJECT_CASE_STUDY.md

### Branch: feature/coverage-scan-agent

### What Was Built

**Matched-games section in Telegram report**: Each matched pair now stores `winner_description` and `pinnacle_name` (`"{home} vs {away}"`) in the pair dict. `orphan_report[sport]["pairs"]` carries these into `send_coverage_report()`. The Telegram message now has a "Matched games:" section listing every pair as `Hebrew → Pinnacle English name | kickoff` — lets you visually spot false positives (wrong match) at a glance. Orphan section below it catches false negatives (should have matched but didn't). Both failure modes visible in one message without touching logs.

**`PROJECT_CASE_STUDY.md`**: Comprehensive technical reference document covering architecture, development process, all design decisions, debugging stories, AI usage, and resume/interview material.

### Final Test Results (after matched-games addition)

31 total matched pairs (soccer 20, basketball 8, baseball 3), 0 orphan games. Telegram report sent at 2247 chars.

---

## 2026-06-13 — Coverage Scan: Orphan Report, Telegram, Fallback Matching

### Branch: feature/coverage-scan-agent

### What Was Built

**Orphan report** (`data/orphans_{date}.json`) — debug file written alongside `coverage_{date}.json` every run:
- `orphan_leagues`: Winner leagues the agent skipped (no Pinnacle equivalent). Listed with game count. Individual games NOT listed here.
- `orphan_games`: games from matched leagues that had no Pinnacle match. Includes `reason` (`no_candidates` or `no_match`), `pinnacle_league` (English Pinnacle key title), and for `no_match`, the full `candidates` list.
- All times in Israel local time (`+03:00`).

**Telegram coverage report**: `send_coverage_report()` in `telegram_bot.py` — sent after each scan. Shows matched/unmatched per sport, then all orphan games with the Hebrew→Pinnacle league mapping and Pinnacle candidates. Single message, truncated at 4000 chars.

**Wrong-key retry logic**: When majority of games in a league have `no_candidates` (i.e. `no_cand_count > len(unmatched) - no_cand_count`), the tool response includes `warning=all_no_candidates` and the sport agent retries with a different Pinnacle key. `already_retried: set[str]` prevents infinite loops — on second failure the league is moved to orphan_leagues. Catches שבדית שלישית→Superettan correctly even when some Superettan games accidentally fall in the ±15min window.

**Single-team fallback** (`_match_single_team`): When Haiku returns NO_MATCH for a full Hebrew description, the function retries using only the home team name, then only the away team name. Fixes ambiguous cases like "אילבס - טורון" where "טורון" (Turku) appears in two candidates — "אילבס" alone unambiguously maps to Ilves Tampere. Logged as `[CoverageScan] Single-team fallback (home): '...' → matched`.

### Bugs Fixed

**Haiku ID truncation (max_tokens=128)**: Haiku reasons before answering (~100 tokens), leaving no room for the 32-char hex ID. Fixed: raised `max_tokens` to 256 and moved format instruction to top of prompt.

**Haiku returns ID with explanation text**: Fixed by extracting the 32-char hex ID with `re.search(r'\b([0-9a-f]{32})\b', raw)` instead of using the raw response. `NO_MATCH` also checked anywhere in the text.

**Outright markets sent to Haiku**: Soccer includes specials like "הזוכה במונדיאל 2026". Fixed with `_is_game(description)` — rejects descriptions with no ` - ` separator or a 4-digit year on either side. Real match descriptions ("ארגנטינה - צרפת") pass unaffected.

### Final Test Results (2026-06-13)

| Sport | Matched pairs | Orphan leagues | Orphan games |
|---|---|---|---|
| Soccer | 18 | 28 | 0 |
| Basketball | 8 | 20 | 0 |
| Baseball | 3 | 0 | 0 |
| Tennis | 0 | 12 | 0 |
| **Total** | **29** | **60** | **0** |

---

## 2026-06-12 — Coverage Scan Agent

### Branch: feature/coverage-scan-agent

### Goal
Build a daily multi-sport coverage scan that finds all Winner↔Pinnacle game matches across Soccer, Basketball, Tennis, and Baseball without a pre-built league map. Output is a flat JSON of matched game pairs ready for future odds lookup.

### What Was Built

**`agents/coverage_scan_agent.py`** — new file, full implementation:
- **Bootstrap phase**: on first run, classifies Winner sport_ids via a single Haiku call. Saves to `translations.json["winner_sport_ids"]`. Discovered: basketball=227, tennis=239, baseball=226.
- **Orchestrator**: single WinnerScraper run → splits 1497 markets by sport → fires 4 sport agents in parallel via `asyncio.to_thread()`.
- **Per-sport agent**: Claude Haiku agent loop with 3 tools (`fetch_pinnacle_events`, `match_games_in_league`, `return_results`). Agent reasons about league matching itself — no pre-built map.
- **Game matching**: for each unique Winner event, filters Pinnacle candidates to ±15 min window, asks Haiku to match by full Hebrew description.
- **Output**: `data/coverage_{date}.json` — flat array of `{winner_event_id, pinnacle_id, kickoff}`.
- **Zero quota cost**: only uses free `/v4/sports` and `/v4/sports/{key}/events` endpoints. `pinnacle_id` is the stable hex ID for future odds lookup via `/v4/sports/{key}/events/{id}/odds`.

### Bugs Fixed During Testing

**Duplicate matches (critical)**: Winner returns ~40 markets per event (1X2, totals, handicaps). `_match_games_in_league` was calling Haiku once per market, producing 303 records for 36 actual games. Fixed by deduplicating by `event_id` before the Haiku loop.

**`id=` prefix from Haiku**: candidates were formatted as `id=<hex> home vs away time` — Haiku sometimes echoed back `id=<hex>` instead of the bare hex string. Fixed by removing the `id=` prefix from the candidate format and adding a strip-prefix safety net.

### Test Results (after fixes)

| Sport | Winner markets | Unique games matched |
|---|---|---|
| Soccer | 580 | ~20 (World Cup qualifiers, Copa America) |
| Basketball | 65 | ~7 (NBA) |
| Baseball | 15 | ~7 (MLB) |
| Tennis | 34 | 0 (no active Pinnacle tennis events) |
| **Total** | **1497** | **37** |

Output: `data/coverage_2026-06-12.json` — 37 records, 37 unique games, 0 duplicates.

### Pending
- Wire into `scheduler.py` at 08:00 IL (separate task)

---

## 2026-06-12 — Matching Service Experiment

### Branch: feature/matching-service → master

---

## What Was Attempted

### Goal
Improve the matching service on `feature/matching-service` to replace `matcher.py`. The new service used time-window search + LLM disambiguation across all sports (Soccer, Baseball, Basketball, Tennis).

### Experiments Run

**1. Baseline — time-window only (no embedding)**
- 11 matches / 87 Winner games

**2. Embedding pre-filter (multilingual model)**
- Added `sentence-transformers` with `paraphrase-multilingual-MiniLM-L12-v2`
- Hebrew queries scored poorly against English Pinnacle names
- Result: improved to 17 matches (but multilingual model is 470MB, slow)

**3. LLM translation + English-only embedding**
- Translate Hebrew team names via Claude Haiku before embedding
- Switch to `all-MiniLM-L6-v2` (80MB, English-only)
- Translation prompt issues: Haiku returned long reasoning chains instead of just team names
- Fixed with: system prompt, prefill (`"English:"`), max_tokens=10, first-line extraction
- Result: dropped back to 11 matches — translation quality wasn't the bottleneck

**4. Rolled back everything**
- Removed sentence-transformers, embedding code, translation step
- Returned to pure time-window + LLM matching
- 11 matches — same as baseline

---

## Key Findings

- **Embedding adds complexity without benefit.** The LLM disambiguator already sees Hebrew + English candidates and reasons about phonetics directly — it's the reliable component.
- **The feature branch approach has too much noise.** Without league-level filtering, it sends games from leagues Pinnacle doesn't cover at all, producing many "No Pinnacle coverage" hits.
- **matcher.py (master) is better** because it filters by `WINNER_LEAGUE_MAP` first — only games from known Pinnacle-covered leagues reach the LLM.
- **11/87 is the ceiling for the feature branch approach** given today's Pinnacle coverage of Winner's games. Not a matching algorithm failure — most unmatched games are genuinely not on Pinnacle.
- **WNBA disambiguation works despite wrong translations.** "וושינגטון" → "Washington Wizards" (wrong) but LLM still matched Washington Mystics correctly from the Hebrew.

---

## Decision

Abandoned `feature/matching-service`. Deleted the branch. Returned to `master`.

The all-sports matching concept is valid but needs a better foundation — league filtering needs to be extended to cover Basketball, Baseball, Tennis before the time-window approach can replace `matcher.py`.

---

## Next Steps (future sessions)

- Extend `WINNER_LEAGUE_MAP` in `translations.json` to cover Basketball, Baseball, Tennis leagues
- Or: Keep `matcher.py` for Soccer and add parallel sport-specific matchers for other sports
- Orphan log: save unmatched Winner games and unmatched Pinnacle games to `data/orphans_{date}.json` for debugging

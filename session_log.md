# Session Log

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

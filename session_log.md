# Session Log — 2026-06-10 / 2026-06-11

## Branch: `feature/matching-service`

---

## What Was Built

### New matching service (`matching_service.py` + `vector_db.py`)

Replaced the old matching approach (manual Hebrew league mappings + per-team Claude translation, soccer-only) with a time-based + LLM approach that covers all sports.

**How it works:**
1. `build_daily_db()` — calls the free Pinnacle `/events` endpoint (no quota cost), indexes all ~490 events into `data/vector_db/{date}.json`
2. `get_today_matches()` — scrapes Winner, deduplicates games, for each game finds Pinnacle events within ±30 min in the same sport group, asks Claude Haiku to confirm the match
3. Every candidate is routed through the LLM — even when only one exists — to prevent false positives

**Live test results:** ~25 matches per run across Soccer, MLB Baseball, NBA/WNBA Basketball, WTA Tennis.

---

## Changes Made

| Commit | Description |
|--------|-------------|
| `5d846e1` | Add all-sports matching service — initial implementation |
| `3df9771` | Strip dead weight: remove sentence-transformers (embeddings never used), delete PLAYBOOK.md, update .gitignore |
| `61b2a74` | Update README to reflect current vs. in-progress state; test_matching saves JSON output |
| `7905451` | Map all discovered Winner sport IDs to Pinnacle groups (226=Baseball, 227=Basketball, 239=Tennis, 1100=Soccer) |

---

## Key Findings

**sentence-transformers was dead weight** — `vector_db.py` loaded a ~500MB model and computed embeddings, but `get_by_time()` (the only method called) never used them. Removed entirely. `vector_db.py` shrank from 193 → 113 lines.

**Winner sport IDs discovered:**
- 226 = Baseball
- 227 = Basketball
- 239 = Tennis
- 240 = Soccer
- 1100 = Soccer (European cups)

**False positive fix** — originally single-candidate matches were auto-accepted. Fixed by routing all candidates through LLM. Caught a case where a Faroese team was being matched to a Spanish second division game.

**Handball** — Pinnacle only has `handball_germany_bundesliga` (inactive, off-season). Winner also has no handball markets currently. Revisit in September.

---

## What's Next (not yet done)

1. Wire `matching_service.py` into `scheduler.py` to replace `matcher.py`
2. Delete `matcher.py` and simplify/replace `daily_prep_agent.py`
3. Add miss diagnostics to JSON output (no_coverage / no_llm_match breakdown)

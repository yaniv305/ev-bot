# /test-pipeline

Run the full EV Bot pipeline with widened time windows for testing purposes.
Winner window: 24 hours (1440 minutes). Pinnacle window: 24 hours (1440 minutes).
Production code must not be modified — use monkey-patching in a temporary script.

## Steps

1. Write a temporary Python script `_test_pipeline_run.py` in the project root with the following logic:

```python
import asyncio, json, logging, sys, time
sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.INFO, format="%(message)s")

from datetime import datetime, timedelta, timezone
from collections import Counter, defaultdict

# --- Monkey-patch Winner window to 24 h (no production file changes) ---
import winner_scraper as _ws
_orig_filter = _ws._filter_upcoming_football
def _wide_filter(markets):
    now = datetime.now(tz=timezone.utc)
    cutoff = now + timedelta(minutes=1440)
    return [m for m in markets
            if m["sport_id"] == 240
            and m.get("kickoff")
            and now <= datetime.fromisoformat(m["kickoff"]) <= cutoff]
_ws._filter_upcoming_football = _wide_filter

# --- Monkey-patch Pinnacle window to 24 h ---
import matcher as _matcher
_matcher._PINNACLE_WINDOW_MIN = 1440

from winner_scraper import WinnerScraper, _filter_upcoming_football
from pinnacle_client import get_pinnacle_odds, WINNER_LEAGUE_MAP
from matcher import match_markets

gemini_errors = []
_orig_translate = _matcher._translate_team
def _tracking_translate(heb_name):
    result = _orig_translate(heb_name)
    if result is None:
        gemini_errors.append(heb_name)
    return result
_matcher._translate_team = _tracking_translate

no_match_log = []
import logging as _logging
class _NoMatchHandler(_logging.Handler):
    def emit(self, record):
        if "[NO MATCH]" in record.getMessage():
            no_match_log.append(record.getMessage())
_matcher.log.addHandler(_NoMatchHandler())

async def main():
    print("=" * 60)
    print("EV Bot — Pipeline Test  (windows: Winner=24h, Pinnacle=24h)")
    print("=" * 60)

    # Step 1: fetch Winner
    async with WinnerScraper(headless=True) as s:
        result = await s.get_all_markets()
    winner_markets = _filter_upcoming_football(result["markets"])
    leagues = Counter(m["league"] for m in winner_markets)

    print(f"\n[Winner] {len(winner_markets)} markets in 24-hour window")
    print(f"         {len(leagues)} unique leagues\n")

    mapped_keys = {}
    print(f"{'Hebrew League':<40}  {'Sport Key / Status'}")
    print("-" * 75)
    for league, count in leagues.most_common():
        key = WINNER_LEAGUE_MAP.get(league)
        status = key if key else "NOT MAPPED"
        mapped_keys[league] = key
        print(f"  {league:<38}  {status}  ({count} markets)")

    # Step 2: fetch Pinnacle
    sport_keys = list({v for v in mapped_keys.values() if v})
    print(f"\n[Pinnacle] Fetching {len(sport_keys)} sport key(s)")
    pinnacle_markets = get_pinnacle_odds(sport_keys)

    by_key = Counter(m["sport_key"] for m in pinnacle_markets)
    print(f"[Pinnacle] {len(pinnacle_markets)} matches returned")
    for key, count in by_key.most_common():
        print(f"  {key}  ({count} matches)")

    # Step 3: match
    print()
    pairs = match_markets(winner_markets, pinnacle_markets)

    # Summary
    print(f"\n{'=' * 60}")
    print(f"MATCHED PAIRS: {len(pairs)}")
    print(f"{'=' * 60}")
    for p in pairs:
        w, pinn = p["winner"], p["pinnacle"]
        print(f"\n  Winner  : {w['description']}  ({w['league']})")
        print(f"  Pinnacle: {pinn['home_team']} vs {pinn['away_team']}")
        print(f"  Kickoff : {w['kickoff']}")
        print(f"  Odds W  : ", end="")
        for o in w["outcomes"]:
            print(f"{o['desc']}={o['price']}", end="  ")
        print()
        print(f"  Odds P  : H={pinn['home_odds']}  D={pinn['draw_odds']}  A={pinn['away_odds']}")

    if no_match_log:
        print(f"\nNO MATCH ({len(no_match_log)}):")
        for msg in no_match_log:
            print(f"  {msg}")

    if gemini_errors:
        print(f"\nGEMINI TRANSLATION ERRORS ({len(gemini_errors)}):")
        for name in gemini_errors:
            print(f"  {name!r}")
    else:
        print("\nGemini: no errors")

    print(f"\n{'=' * 60}")
    print("Done.")
    print(f"{'=' * 60}")

asyncio.run(main())
```

2. Run the script using the project virtualenv:
   ```
   .venv/Scripts/python _test_pipeline_run.py
   ```

3. Print all output to the user.

4. Delete `_test_pipeline_run.py` after the run completes (whether it succeeded or failed).

5. Do not edit `winner_scraper.py`, `matcher.py`, `pinnacle_client.py`, or `translations.json`.

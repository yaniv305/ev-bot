# /test-ev

Run the full EV Bot pipeline with a 24-hour window and display all +EV opportunities.
Winner window: 24 hours (1440 minutes). Pinnacle window: 24 hours (1440 minutes).
Production code must not be modified — use monkey-patching in a temporary script.

## Steps

1. Write a temporary Python script `_test_ev_run.py` in the project root with the following logic:

```python
import asyncio, logging, sys
sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.INFO, format="%(message)s")

from datetime import datetime, timedelta, timezone

# --- Monkey-patch Winner window to 24 h ---
import winner_scraper as _ws
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
from ev_calculator import calculate_ev

# Track Claude translation errors
claude_errors = []
_orig_translate = _matcher._translate_team
def _tracking_translate(heb_name, pinnacle_teams):
    result = _orig_translate(heb_name, pinnacle_teams)
    if result is None:
        claude_errors.append(heb_name)
    return result
_matcher._translate_team = _tracking_translate

# Track NO MATCH log lines
no_match_log = []
import logging as _logging
class _NoMatchHandler(_logging.Handler):
    def emit(self, record):
        if "[NO MATCH]" in record.getMessage():
            no_match_log.append(record.getMessage())
_matcher.log.addHandler(_NoMatchHandler())

async def main():
    print("=" * 60)
    print("EV Bot — EV Test  (windows: Winner=24h, Pinnacle=24h)")
    print("=" * 60)

    # Step 1: fetch Winner
    async with WinnerScraper(headless=True) as s:
        result = await s.get_all_markets()
    winner_markets = _filter_upcoming_football(result["markets"])
    print(f"\n[Winner] {len(winner_markets)} markets in 24-hour window")

    # Step 2: fetch Pinnacle
    sport_keys = list({
        WINNER_LEAGUE_MAP[m["league"]]
        for m in winner_markets
        if WINNER_LEAGUE_MAP.get(m["league"])
    })
    pinnacle_markets = get_pinnacle_odds(sport_keys)
    print(f"[Pinnacle] {len(pinnacle_markets)} matches across {len(sport_keys)} leagues")

    # Step 3: match
    pairs = match_markets(winner_markets, pinnacle_markets)
    print(f"[Matcher] {len(pairs)} matched pairs")

    # Step 4: EV
    alerts = calculate_ev(pairs)

    # Step 5: print alerts
    print(f"\n{'=' * 60}")
    if not alerts:
        print("No +EV opportunities found.")
    else:
        print(f"+EV OPPORTUNITIES ({len(alerts)} found)")
        print("=" * 60)
        for a in alerts:
            print(
                f"\n  {a['match']}"
                f"  [{a['israel_time']} IL]"
            )
            print(
                f"  {a['outcome']:5s}  "
                f"Winner={a['winner_odds']:.2f}  "
                f"Fair={a['pinnacle_fair_odds']:.2f}  "
                f"EV=+{a['ev_pct']}%"
            )

    # Step 6: print NO MATCH
    if no_match_log:
        print(f"\n{'=' * 60}")
        print(f"NO MATCH ({len(no_match_log)}):")
        for msg in no_match_log:
            print(f"  {msg}")

    # Step 7: print Claude errors
    if claude_errors:
        print(f"\n{'=' * 60}")
        print(f"CLAUDE TRANSLATION ERRORS ({len(claude_errors)}):")
        for name in claude_errors:
            print(f"  {name!r}")
    else:
        print("\nClaude: no translation errors")

    print(f"\n{'=' * 60}")

asyncio.run(main())
```

2. Run the script using the project virtualenv:
   ```
   .venv/Scripts/python _test_ev_run.py
   ```

3. Print all output to the user.

4. Delete `_test_ev_run.py` after the run completes (whether it succeeded or failed).

5. Do not edit `winner_scraper.py`, `matcher.py`, `pinnacle_client.py`, `ev_calculator.py`, or `translations.json`.

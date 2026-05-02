# /check-leagues

Fetch current Winner markets and check which Hebrew league names are mapped in translations.json.
Uses a 24-hour window to maximize league coverage.
Production code must not be modified — use a temporary script.

## Steps

1. Write a temporary Python script `_check_leagues_run.py` in the project root with the following logic:

```python
import asyncio, sys, json, pathlib
sys.stdout.reconfigure(encoding="utf-8")
import logging
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

from winner_scraper import WinnerScraper, _filter_upcoming_football

# Load translations
translations_path = pathlib.Path("translations.json")
with translations_path.open(encoding="utf-8") as f:
    data = json.load(f)
league_map = data["winner_league_to_sport_key"]

async def main():
    async with WinnerScraper(headless=True) as s:
        result = await s.get_all_markets()

    winner_markets = _filter_upcoming_football(result["markets"])
    leagues = sorted({m["league"] for m in winner_markets})

    mapped     = {l: league_map[l] for l in leagues if l in league_map}
    not_mapped = [l for l in leagues if l not in league_map]

    # Suggest possible matches for unmapped leagues:
    # Check if adding or removing ", כדורגל" suffix matches a known key
    all_keys = set(league_map.keys())
    suggestions = {}
    for l in not_mapped:
        if l + ", כדורגל" in all_keys:
            suggestions[l] = f'close match in translations.json: "{l + ", כדורגל"}" → {league_map[l + ", כדורגל"]}'
        elif l.removesuffix(", כדורגל") in all_keys:
            base = l.removesuffix(", כדורגל")
            suggestions[l] = f'close match in translations.json: "{base}" → {league_map[base]}'
        else:
            # Check if any key contains the league name or vice versa
            for key in all_keys:
                if l in key or key in l:
                    suggestions[l] = f'partial match: "{key}" → {league_map[key]}'
                    break

    print(f"\n{'=' * 60}")
    print(f"Winner leagues — 24-hour window")
    print(f"Total: {len(leagues)}  |  Mapped: {len(mapped)}  |  Not mapped: {len(not_mapped)}")
    print(f"{'=' * 60}")

    print(f"\nMAPPED ({len(mapped)}):")
    for league, key in sorted(mapped.items()):
        print(f"  {league}  →  {key}")

    print(f"\nNOT MAPPED ({len(not_mapped)}):")
    if not not_mapped:
        print("  (none)")
    for league in not_mapped:
        suggestion = suggestions.get(league)
        if suggestion:
            print(f"  {league}")
            print(f"    ⚠ {suggestion}")
        else:
            print(f"  {league}")

    print(f"\n{'=' * 60}")

asyncio.run(main())
```

2. Run the script using the project virtualenv:
   ```
   .venv/Scripts/python _check_leagues_run.py
   ```

3. Print all output to the user.

4. Delete `_check_leagues_run.py` after the run completes (whether it succeeded or failed).

5. Do not edit `winner_scraper.py`, `matcher.py`, `pinnacle_client.py`, or `translations.json`.

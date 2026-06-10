import asyncio
import dataclasses
import json
import logging
import pathlib

from matching_service import build_daily_db, get_today_matches

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)

print("=" * 60)
print("STEP 1 — Building daily Pinnacle DB")
print("=" * 60)
build_daily_db()

print()
print("=" * 60)
print("STEP 2 — Matching Winner games")
print("=" * 60)
games = asyncio.run(get_today_matches())

print()
print(f"Total matched: {len(games)}")

if games:
    print()
    print("-" * 60)
    for g in games:
        print(f"  Winner : {g.winner_home_raw} - {g.winner_away_raw}")
        print(f"  Pinnacle: {g.pinnacle_home} vs {g.pinnacle_away}")
        print(f"  Sport  : {g.sport_key}")
        print(f"  Kickoff: {g.commence_time}")
        print()

out_path = pathlib.Path(__file__).parent / "data" / "matched_games.json"
out_path.parent.mkdir(parents=True, exist_ok=True)
with open(out_path, "w", encoding="utf-8") as f:
    json.dump([dataclasses.asdict(g) for g in games], f, ensure_ascii=False, indent=2)
print(f"Saved to {out_path}")

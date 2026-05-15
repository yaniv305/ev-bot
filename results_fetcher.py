"""
results_fetcher.py — Fetch finished match results via The Odds API scores endpoint.
"""
import json
import logging
import os
import pathlib
from difflib import SequenceMatcher
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

_BASE_URL      = "https://api.the-odds-api.com/v4"
_TRANSLATIONS  = pathlib.Path(__file__).parent / "translations.json"
_FUZZY_THRESHOLD = 0.90


def _load_sport_keys() -> list[str]:
    with _TRANSLATIONS.open(encoding="utf-8") as f:
        data = json.load(f)
    return sorted({v for v in data.get("winner_league_to_sport_key", {}).values() if v})


def _fetch_scores(sport_key: str, api_key: str) -> list[dict]:
    try:
        resp = requests.get(
            f"{_BASE_URL}/sports/{sport_key}/scores/",
            params={"apiKey": api_key, "daysFrom": 1},
            timeout=10,
        )
        if not resp.ok:
            log.warning("[ResultsFetcher] HTTP %s for %s", resp.status_code, sport_key)
            return []
        return resp.json()
    except Exception as exc:
        log.error("[ResultsFetcher] Error fetching scores for %s: %s", sport_key, exc)
        return []


def _determine_result(home_team: str, away_team: str, scores: list[dict]) -> Optional[str]:
    try:
        home_score = next(int(s["score"]) for s in scores if s["name"] == home_team)
        away_score = next(int(s["score"]) for s in scores if s["name"] == away_team)
    except (StopIteration, ValueError, TypeError):
        return None
    if home_score > away_score:
        return "Home"
    if away_score > home_score:
        return "Away"
    return "Draw"


def _fuzzy_match(name: str, candidates: list[str]) -> Optional[str]:
    best, best_score = None, 0.0
    for candidate in candidates:
        score = SequenceMatcher(None, name.lower(), candidate.lower()).ratio()
        if score > best_score:
            best, best_score = candidate, score
    return best if best_score >= _FUZZY_THRESHOLD else None


def get_results_batch(pending: list[dict]) -> dict[int, Optional[str]]:
    """
    Fetch results for pending alerts from The Odds API scores endpoint.

    Args:
        pending: rows from get_pending_results() — each has event_id, home, away.

    Returns:
        {event_id: "Home"|"Draw"|"Away"|None}
    """
    if not pending:
        return {}

    api_key = os.getenv("ODDS_API_KEY")
    if not api_key:
        log.error("[ResultsFetcher] ODDS_API_KEY not set")
        return {row["event_id"]: None for row in pending}

    results: dict[int, Optional[str]] = {row["event_id"]: None for row in pending}

    # Fetch completed games across all mapped leagues
    completed: dict[tuple[str, str], str] = {}
    for sport_key in _load_sport_keys():
        for game in _fetch_scores(sport_key, api_key):
            if not game.get("completed"):
                continue
            home = game.get("home_team", "")
            away = game.get("away_team", "")
            result = _determine_result(home, away, game.get("scores") or [])
            if result is not None:
                completed[(home, away)] = result

    if not completed:
        return results

    all_home_teams = [k[0] for k in completed]

    for row in pending:
        db_home, db_away = row["home"], row["away"]

        # Exact match
        if (db_home, db_away) in completed:
            results[row["event_id"]] = completed[(db_home, db_away)]
            log.info("[ResultsFetcher] %s vs %s → %s (exact)",
                     db_home, db_away, results[row["event_id"]])
            continue

        # Fuzzy match on home team, then constrain away candidates to that home
        matched_home = _fuzzy_match(db_home, all_home_teams)
        if matched_home is None:
            continue
        away_candidates = [k[1] for k in completed if k[0] == matched_home]
        matched_away = _fuzzy_match(db_away, away_candidates)
        if matched_away and (matched_home, matched_away) in completed:
            results[row["event_id"]] = completed[(matched_home, matched_away)]
            log.info("[ResultsFetcher] %s vs %s → %s (fuzzy: %s vs %s)",
                     db_home, db_away, results[row["event_id"]], matched_home, matched_away)

    return results

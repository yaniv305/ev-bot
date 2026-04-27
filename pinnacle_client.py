"""
pinnacle_client.py — Pinnacle 1X2 odds via The Odds API
"""
import json
import logging
import os
import pathlib

import requests
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

_API_KEY   = os.getenv("ODDS_API_KEY")
_BASE_URL  = "https://api.the-odds-api.com/v4"
_BOOKMAKER = "pinnacle"
_REGIONS   = "eu"
_ODDS_FMT  = "decimal"

_TRANSLATIONS = pathlib.Path(__file__).parent / "translations.json"

def _load_league_map() -> dict[str, str | None]:
    with _TRANSLATIONS.open(encoding="utf-8") as f:
        return json.load(f)["winner_league_to_sport_key"]

# Maps Winner.co.il Hebrew league names → Odds API sport keys (None = not on The Odds API).
# Usage: WINNER_LEAGUE_MAP.get(winner_market["league"])
# Edit translations.json to add or correct entries.
WINNER_LEAGUE_MAP: dict[str, str | None] = _load_league_map()


def _log_quota(resp: requests.Response) -> None:
    used      = resp.headers.get("x-requests-used", "?")
    remaining = resp.headers.get("x-requests-remaining", "?")
    log.info("[OddsAPI] Quota — used: %s  remaining: %s", used, remaining)


def _find_price(outcomes: list[dict], name: str) -> float | None:
    for o in outcomes:
        if o.get("name") == name:
            return float(o["price"])
    return None


def get_pinnacle_odds(league_keys: list[str]) -> list[dict]:
    """
    Fetch 1X2 (h2h) Pinnacle odds for the given Odds API sport keys.

    Args:
        league_keys: Odds API sport key strings,
                     e.g. ["soccer_epl", "soccer_iceland_premier_league"].
                     Use WINNER_LEAGUE_MAP to convert Winner league names.

    Returns:
        List of dicts:
            sport_key      str
            home_team      str
            away_team      str
            commence_time  str   (UTC ISO 8601)
            home_odds      float
            draw_odds      float | None
            away_odds      float
    """
    if not _API_KEY:
        raise ValueError("ODDS_API_KEY is not set. Add it to .env.")

    results: list[dict] = []

    for sport_key in league_keys:
        url    = f"{_BASE_URL}/sports/{sport_key}/odds"
        params = {
            "apiKey":     _API_KEY,
            "regions":    _REGIONS,
            "markets":    "h2h",
            "bookmakers": _BOOKMAKER,
            "oddsFormat": _ODDS_FMT,
        }

        try:
            resp = requests.get(url, params=params, timeout=10)
        except requests.RequestException as exc:
            log.error("[OddsAPI] Network error for %s: %s", sport_key, exc)
            continue

        _log_quota(resp)

        if resp.status_code == 401:
            log.error("[OddsAPI] Invalid API key — aborting.")
            break
        if resp.status_code == 422:
            log.warning("[OddsAPI] Unknown sport key %r — skipping.", sport_key)
            continue
        if resp.status_code == 429:
            log.error("[OddsAPI] Quota exhausted — aborting.")
            break
        if not resp.ok:
            log.error("[OddsAPI] HTTP %s for %s: %s",
                      resp.status_code, sport_key, resp.text[:200])
            continue

        try:
            events = resp.json()
        except ValueError as exc:
            log.error("[OddsAPI] JSON parse error for %s: %s", sport_key, exc)
            continue

        match_count = 0
        for event in events:
            pinnacle = next(
                (bm for bm in event.get("bookmakers", []) if bm["key"] == _BOOKMAKER),
                None,
            )
            if pinnacle is None:
                continue

            h2h = next(
                (m for m in pinnacle.get("markets", []) if m["key"] == "h2h"),
                None,
            )
            if h2h is None:
                continue

            outcomes  = h2h.get("outcomes", [])
            home_odds = _find_price(outcomes, event["home_team"])
            away_odds = _find_price(outcomes, event["away_team"])
            draw_odds = _find_price(outcomes, "Draw")

            if home_odds is None or away_odds is None:
                log.warning("[OddsAPI] Missing home/away price for %s vs %s — skipping.",
                            event.get("home_team"), event.get("away_team"))
                continue

            results.append({
                "sport_key":     sport_key,
                "home_team":     event["home_team"],
                "away_team":     event["away_team"],
                "commence_time": event["commence_time"],
                "home_odds":     home_odds,
                "draw_odds":     draw_odds,
                "away_odds":     away_odds,
            })
            match_count += 1

        log.info("[OddsAPI] %s — %d Pinnacle matches", sport_key, match_count)

    return results

"""
pinnacle_client.py — Fetch Pinnacle odds via The Odds API (event-specific endpoint).
"""
import logging
import os

import requests
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

_API_KEY   = os.getenv("ODDS_API_KEY")
_BASE_URL  = "https://api.the-odds-api.com/v4"
_BOOKMAKER = "pinnacle"
_REGIONS   = "eu"
_ODDS_FMT  = "decimal"


def _log_quota(resp: requests.Response) -> None:
    used      = resp.headers.get("x-requests-used", "?")
    remaining = resp.headers.get("x-requests-remaining", "?")
    log.info("[OddsAPI] Quota — used: %s  remaining: %s", used, remaining)


def _find_price(outcomes: list[dict], name: str) -> float | None:
    for o in outcomes:
        if o.get("name") == name:
            return float(o["price"])
    return None



def get_event_odds(sport_key: str, event_id: str) -> dict | None:
    """
    Fetch h2h + totals + alternate_totals + btts for one event.
    GET /v4/sports/{sport_key}/events/{event_id}/odds?markets=h2h,totals,alternate_totals,btts
    Cost: 4 quota per call (4 markets × 1 region).
    Returns parsed dict or None if event not found / Pinnacle not present.
    """
    if not _API_KEY:
        raise ValueError("ODDS_API_KEY is not set. Add it to .env.")

    url = f"{_BASE_URL}/sports/{sport_key}/events/{event_id}/odds"
    params = {
        "apiKey":     _API_KEY,
        "regions":    _REGIONS,
        "markets":    "h2h,totals,alternate_totals,btts",
        "bookmakers": _BOOKMAKER,
        "oddsFormat": _ODDS_FMT,
    }

    try:
        resp = requests.get(url, params=params, timeout=10)
    except requests.RequestException as exc:
        log.error("[OddsAPI] Network error for event %s: %s", event_id, exc)
        return None

    _log_quota(resp)

    if resp.status_code == 404:
        log.warning("[OddsAPI] Event %s not found (404)", event_id)
        return None
    if not resp.ok:
        log.error("[OddsAPI] HTTP %s for event %s: %s", resp.status_code, event_id, resp.text[:200])
        return None

    event = resp.json()
    pinnacle = next(
        (bm for bm in event.get("bookmakers", []) if bm["key"] == _BOOKMAKER),
        None,
    )
    if pinnacle is None:
        log.warning("[OddsAPI] Pinnacle not in response for event %s", event_id)
        return None

    markets = {m["key"]: m for m in pinnacle.get("markets", [])}

    # h2h
    h2h_outcomes = markets.get("h2h", {}).get("outcomes", [])
    home_odds = _find_price(h2h_outcomes, event["home_team"])
    draw_odds = _find_price(h2h_outcomes, "Draw")
    away_odds = _find_price(h2h_outcomes, event["away_team"])

    # totals + alternate_totals — merge and deduplicate by point
    totals_by_point: dict[float, dict] = {}
    for market_key in ("totals", "alternate_totals"):
        for o in markets.get(market_key, {}).get("outcomes", []):
            point = o.get("point")
            if point is None:
                continue
            entry = totals_by_point.setdefault(point, {})
            if o["name"] == "Over":
                entry["over_odds"] = float(o["price"])
            elif o["name"] == "Under":
                entry["under_odds"] = float(o["price"])

    totals = [
        {"point": pt, "over_odds": v["over_odds"], "under_odds": v["under_odds"]}
        for pt, v in sorted(totals_by_point.items())
        if "over_odds" in v and "under_odds" in v
    ]

    # btts
    btts_outcomes = markets.get("btts", {}).get("outcomes", [])
    btts_yes = _find_price(btts_outcomes, "Yes")
    btts_no  = _find_price(btts_outcomes, "No")

    return {
        "event_id":      event["id"],
        "sport_key":     event["sport_key"],
        "home_team":     event["home_team"],
        "away_team":     event["away_team"],
        "home_odds":     home_odds,
        "draw_odds":     draw_odds,
        "away_odds":     away_odds,
        "totals":        totals,
        "btts_yes_odds": btts_yes,
        "btts_no_odds":  btts_no,
    }

"""
matcher.py — Match Winner games against Pinnacle games for EV calculation.
See PLAYBOOK.md for full matching conditions.
"""
import json
import logging
import os
import pathlib
from datetime import datetime, timedelta, timezone
from typing import Optional

import anthropic
from dotenv import load_dotenv

from pinnacle_client import WINNER_LEAGUE_MAP

load_dotenv()

log = logging.getLogger(__name__)

_TRANSLATIONS = pathlib.Path(__file__).parent / "translations.json"

# Thresholds — keep in sync with PLAYBOOK.md
_PINNACLE_WINDOW_MIN = 45                    # condition 3: Pinnacle kickoff window
_KICKOFF_DELTA_MAX   = timedelta(minutes=15) # condition 4: max kickoff difference


# ── translations.json helpers ──────────────────────────────────────────────────

def _load_translations() -> dict:
    with _TRANSLATIONS.open(encoding="utf-8") as f:
        return json.load(f)


def _save_translations(data: dict) -> None:
    """Atomic write via temp-file rename — a crash cannot corrupt the file."""
    tmp = _TRANSLATIONS.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(_TRANSLATIONS)


# ── Team name translation ──────────────────────────────────────────────────────

def _translate_team(heb_name: str, pinnacle_teams: list[str]) -> Optional[str]:
    """
    Return the exact Pinnacle team name matching the Hebrew name.

    1. Checks translations.json["team_name_cache"] — free, O(1).
    2. On cache miss, asks Claude Haiku to pick the correct name from
       pinnacle_teams. Caches and persists the result immediately.
       Returns None if Claude returns NO_MATCH or an error occurs.
    """
    data  = _load_translations()
    cache = data.setdefault("team_name_cache", {})

    if heb_name in cache:
        return cache[heb_name]

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("[Matcher] ANTHROPIC_API_KEY not set — cannot translate %r", heb_name)
        return None

    prompt = (
        f"Here is a Hebrew football team name: '{heb_name}'\n"
        f"Here is the list of team names from Pinnacle for this league:\n"
        f"{pinnacle_teams}\n"
        f"Which Pinnacle team name matches the Hebrew name?\n"
        f"Return ONLY the exact Pinnacle name from the list, nothing else.\n"
        f"If no match exists, return NO_MATCH.\n"
        f"IMPORTANT: Return ONLY the exact team name from the list or the word NO_MATCH. "
        f"Do not return any explanation, sentence, or extra text."
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": prompt}],
        )
        result = response.content[0].text.strip()
    except Exception as exc:
        log.error("[Matcher] Claude error translating %r: %s", heb_name, exc)
        return None

    if result == "NO_MATCH":
        return None

    log.info("[Matcher] Translated %r → %r via Claude", heb_name, result)
    cache[heb_name] = result
    _save_translations(data)
    return result


# ── Pinnacle window filter ─────────────────────────────────────────────────────

def _filter_pinnacle_window(pinnacle_markets: list[dict]) -> list[dict]:
    """
    Condition 3: keep Pinnacle matches kicking off within the next 45 minutes.
    The Winner 30-min window is applied upstream in winner_scraper.py.
    """
    now    = datetime.now(tz=timezone.utc)
    cutoff = now + timedelta(minutes=_PINNACLE_WINDOW_MIN)
    result = []
    for m in pinnacle_markets:
        try:
            ko = datetime.fromisoformat(m["commence_time"].replace("Z", "+00:00"))
        except (ValueError, KeyError):
            continue
        if now <= ko <= cutoff:
            result.append(m)
    return result


# ── Description parser ─────────────────────────────────────────────────────────

def _split_description(description: str) -> tuple[str, str]:
    """
    Split a Winner description 'HomeTeam - AwayTeam' into (home, away).
    Returns ('', '') if the separator is not found.
    """
    parts = description.split(" - ", 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return "", ""


# ── Main matcher ───────────────────────────────────────────────────────────────

def match_markets(
    winner_markets: list[dict],
    pinnacle_markets: list[dict],
) -> list[dict]:
    """
    Match Winner games against Pinnacle games for EV calculation.

    Matching conditions (see PLAYBOOK.md):
      1. Same league    — Hebrew name maps to sport key via WINNER_LEAGUE_MAP
      2. Winner window  — already applied upstream (winner_scraper.py, 30 min)
      3. Pinnacle window — kickoff within 45 min from now (applied here)
      4. Kickoff proximity — Winner vs Pinnacle difference ≤ 15 min
      5. Team names     — Claude picks the exact Pinnacle name from the league list

    Only full-time 1X2 markets are matched (market_type contains both "1X2"
    and "תוצאת סיום"). Multiple Winner markets for the same event are
    deduplicated by event_id before matching.

    Args:
        winner_markets:   from WinnerScraper.get_all_markets()["markets"],
                          already filtered to football + 30-min window.
        pinnacle_markets: from get_pinnacle_odds().

    Returns:
        List of {"winner": <dict>, "pinnacle": <dict>} pairs.
    """
    # Condition 3: filter Pinnacle to the 45-min window
    pinnacle_in_window = _filter_pinnacle_window(pinnacle_markets)

    # Index Pinnacle by sport_key for O(1) lookup per league
    pinnacle_by_sport: dict[str, list[dict]] = {}
    for pm in pinnacle_in_window:
        pinnacle_by_sport.setdefault(pm["sport_key"], []).append(pm)

    # Restrict Winner to full-time 1X2; deduplicate by event_id
    seen_events: set[int] = set()
    winner_1x2: list[dict] = []
    for wm in winner_markets:
        mt = wm.get("market_type", "")
        if "1X2" not in mt or "תוצאת סיום" not in mt:
            continue
        if wm["event_id"] in seen_events:
            continue
        seen_events.add(wm["event_id"])
        winner_1x2.append(wm)

    pairs: list[dict] = []

    for wm in winner_1x2:
        # Condition 1: league must map to a Pinnacle sport key
        sport_key = WINNER_LEAGUE_MAP.get(wm["league"])
        if not sport_key:
            continue

        candidates = pinnacle_by_sport.get(sport_key, [])
        if not candidates:
            continue

        # Parse home/away team names from description
        home_heb, away_heb = _split_description(wm["description"])
        if not home_heb or not away_heb:
            log.warning("[Matcher] Unparseable description: %r", wm["description"])
            continue

        # Collect all unique Pinnacle team names in this league for Claude
        pinnacle_teams = sorted({
            name
            for pm in candidates
            for name in (pm["home_team"], pm["away_team"])
        })

        # Condition 5: translate Winner names to exact Pinnacle names via Claude
        home_eng = _translate_team(home_heb, pinnacle_teams)
        away_eng = _translate_team(away_heb, pinnacle_teams)
        if home_eng is None or away_eng is None:
            pinnacle_teams_str = pinnacle_teams
            log.info(
                "[Matcher] [NO MATCH] %s vs %s  (%s)\n  Pinnacle teams in this league: %s",
                home_heb, away_heb, sport_key, pinnacle_teams_str,
            )
            continue

        # Winner kickoff (already UTC-aware from winner_scraper)
        try:
            winner_ko = datetime.fromisoformat(wm["kickoff"])
        except (ValueError, KeyError):
            continue

        # Find the matching Pinnacle game (condition 4: kickoff proximity)
        matched: Optional[dict] = None
        for pm in candidates:
            try:
                pinn_ko = datetime.fromisoformat(pm["commence_time"].replace("Z", "+00:00"))
            except (ValueError, KeyError):
                continue
            if abs(winner_ko - pinn_ko) > _KICKOFF_DELTA_MAX:
                continue
            if home_eng == pm["home_team"] and away_eng == pm["away_team"]:
                matched = pm
                break

        if matched:
            log.info("[Matcher] Matched: %s vs %s  (%s)", home_eng, away_eng, sport_key)
            pairs.append({"winner": wm, "pinnacle": matched})
        else:
            log.info(
                "[Matcher] [NO MATCH] %s vs %s  (%s)\n  Pinnacle teams in this league: %s",
                home_heb, away_heb, sport_key, pinnacle_teams,
            )

    log.info("[Matcher] %d / %d Winner games matched", len(pairs), len(winner_1x2))
    return pairs

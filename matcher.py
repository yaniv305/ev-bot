"""
matcher.py — Match Winner games against Pinnacle games for EV calculation.
See PLAYBOOK.md for full matching conditions.
"""
import json
import logging
import os
import pathlib
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Optional

from google import genai
from dotenv import load_dotenv

from pinnacle_client import WINNER_LEAGUE_MAP

load_dotenv()

log = logging.getLogger(__name__)

_TRANSLATIONS = pathlib.Path(__file__).parent / "translations.json"
_GEMINI_MODEL = "gemini-2.5-flash"

# Thresholds — keep in sync with PLAYBOOK.md
_PINNACLE_WINDOW_MIN   = 45                      # condition 3: Pinnacle kickoff window
_KICKOFF_DELTA_MAX     = timedelta(minutes=15)   # condition 4: max kickoff difference
_FUZZY_THRESHOLD       = 0.90                    # condition 5: min fuzzy name score

# Gemini retry policy (503 = transient high-demand; all other errors fail immediately)
_GEMINI_MAX_RETRIES    = 3
_GEMINI_RETRY_DELAY_S  = 5


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

def _translate_team(heb_name: str) -> Optional[str]:
    """
    Return the Pinnacle English form of a Hebrew team name.

    1. Checks translations.json["team_name_cache"] — free, O(1).
    2. On cache miss, calls Gemini Flash and immediately persists the result.
       Retries up to _GEMINI_MAX_RETRIES times on 503 (transient high demand),
       waiting _GEMINI_RETRY_DELAY_S seconds between attempts.
       Any other error fails immediately without retrying.

    Returns None if translation fails (no API key or all attempts exhausted).
    """
    import time

    data  = _load_translations()
    cache = data.setdefault("team_name_cache", {})

    if heb_name in cache:
        return cache[heb_name]

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        log.error("[Matcher] GEMINI_API_KEY not set — cannot translate %r", heb_name)
        return None

    prompt = (
        f"I am looking up a football team on the bookmaker Pinnacle. "
        f"The team's name in Hebrew is '{heb_name}'. "
        f"This is NOT an Israeli team — it is a foreign team whose name has been transliterated into Hebrew. "
        f"What is the exact English name of this team as it appears on Pinnacle? "
        f"Return ONLY the team name, nothing else."
    )

    client = genai.Client(api_key=api_key)
    eng_name: Optional[str] = None
    for attempt in range(1, _GEMINI_MAX_RETRIES + 2):
        try:
            response = client.models.generate_content(model=_GEMINI_MODEL, contents=prompt)
            eng_name = response.text.strip()
            break
        except Exception as exc:
            if "503" in str(exc) and attempt <= _GEMINI_MAX_RETRIES:
                log.warning("[Matcher] Gemini 503 for %r — retry %d/%d in %ds",
                            heb_name, attempt, _GEMINI_MAX_RETRIES, _GEMINI_RETRY_DELAY_S)
                time.sleep(_GEMINI_RETRY_DELAY_S)
            else:
                log.error("[Matcher] Gemini error translating %r: %s", heb_name, exc)
                return None

    if eng_name is None:
        return None

    log.info("[Matcher] Translated %r → %r via Gemini", heb_name, eng_name)
    cache[heb_name] = eng_name
    _save_translations(data)
    return eng_name


# ── Name matching ──────────────────────────────────────────────────────────────

def _names_match(a: str, b: str) -> bool:
    """True if names match exactly (case-insensitive) or SequenceMatcher ≥ 90%."""
    if a.lower() == b.lower():
        return True
    return SequenceMatcher(None, a.lower(), b.lower()).ratio() >= _FUZZY_THRESHOLD


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
      5. Team names     — exact or fuzzy (≥ 90%) after Hebrew→English translation

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

        # Condition 5 (part 1): translate Winner names to English
        home_eng = _translate_team(home_heb)
        away_eng = _translate_team(away_heb)
        if home_eng is None or away_eng is None:
            continue

        # Winner kickoff (already UTC-aware from winner_scraper)
        try:
            winner_ko = datetime.fromisoformat(wm["kickoff"])
        except (ValueError, KeyError):
            continue

        # Find a matching Pinnacle game
        matched: Optional[dict] = None
        for pm in candidates:
            # Condition 4: kickoff proximity
            try:
                pinn_ko = datetime.fromisoformat(pm["commence_time"].replace("Z", "+00:00"))
            except (ValueError, KeyError):
                continue
            if abs(winner_ko - pinn_ko) > _KICKOFF_DELTA_MAX:
                continue

            # Condition 5 (part 2): team names
            if _names_match(home_eng, pm["home_team"]) and _names_match(away_eng, pm["away_team"]):
                matched = pm
                break

        if matched:
            log.info("[Matcher] Matched: %s vs %s  (%s)", home_eng, away_eng, sport_key)
            pairs.append({"winner": wm, "pinnacle": matched})
        else:
            pinnacle_teams = sorted({
                name
                for pm in candidates
                for name in (pm["home_team"], pm["away_team"])
            })
            log.info(
                "[Matcher] [NO MATCH] %s vs %s  (%s)\n  Pinnacle teams in this league: %s",
                home_heb, away_heb, sport_key, pinnacle_teams,
            )

    log.info("[Matcher] %d / %d Winner games matched", len(pairs), len(winner_1x2))
    return pairs

"""
matching_service.py — Match today's Winner games to Pinnacle games.

Public interface:
    build_daily_db()       — fetch all Pinnacle events, build and save daily index.
                             Call once per morning (daily_prep_agent.py at 11:50 IL).

    get_today_matches()    — scrape Winner, time-match against daily DB, return
                             list[MatchedGame] with one row per confirmed match.
                             Async — call with asyncio.run() or await from async context.

Output contract:
    Each MatchedGame row contains the same game as it appears on both platforms:
        pinnacle_home / pinnacle_away  — exact English strings from Pinnacle
        winner_home_raw / winner_away_raw — exact Hebrew strings from Winner
    No odds, no EV, no Telegram. Pure translation/matching only.
"""

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from pinnacle_client import get_all_sports, get_today_events
from vector_db import DailyVectorDB
from winner_scraper import WinnerScraper

load_dotenv()
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

# Known Winner sport_id → Odds API group hint for narrowing time-window search.
# Games with unknown sport_ids still go through matching — candidates are searched
# across all groups and the LLM identifies the sport from team names.
SPORT_ID_TO_GROUP: dict[int, str] = {
    226:  "Baseball",
    227:  "Basketball",
    239:  "Tennis",
    240:  "Soccer",
    1100: "Soccer",   # European cups (Champions League etc.)
}

# How many seconds either side of the Winner kickoff to search for Pinnacle events.
_TIME_WINDOW_S = 1800  # ±30 minutes


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class MatchedGame:
    winner_event_id:   int
    pinnacle_event_id: str
    sport_key:         str
    pinnacle_home:     str
    pinnacle_away:     str
    winner_home_raw:   str
    winner_away_raw:   str
    commence_time:     str


# ── Public API ────────────────────────────────────────────────────────────────

def build_daily_db() -> None:
    """
    Fetch today's Pinnacle events via the free /events endpoint, build and
    persist the daily index to disk (date-keyed).

    Call once per morning before get_today_matches().
    """
    sports = get_all_sports()
    if not sports:
        log.error("[Matching] build_daily_db: no sports returned from Odds API.")
        return

    events = get_today_events(sports)
    if not events:
        log.error("[Matching] build_daily_db: no events returned from Odds API.")
        return

    db = DailyVectorDB()
    db.build(events)

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    db.save(date_str)
    log.info("[Matching] Daily DB ready for %s", date_str)


async def get_today_matches() -> list[MatchedGame]:
    """
    Scrape Winner, vector-match each game against today's Pinnacle index,
    return a list of confirmed MatchedGame rows.

    Loads today's index from disk (building it on-the-fly if missing).
    Filters Winner markets to: all sports in SPORT_ID_TO_GROUP, kickoff within
    the next 24 hours, deduplicated by game.
    """
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    db = DailyVectorDB()
    if not db.load(date_str):
        log.info("[Matching] No DB for today — building now.")
        build_daily_db()
        if not db.load(date_str):
            log.error("[Matching] Failed to build daily DB — aborting.")
            return []

    # Scrape Winner
    async with WinnerScraper() as scraper:
        result = await scraper.get_all_markets()

    if result.get("error"):
        log.error("[Matching] WinnerScraper error: %s", result["error"])
        return []

    markets: list[dict] = result.get("markets", [])
    log.info("[Matching] Winner returned %d raw markets", len(markets))

    # Filter: supported sport, valid kickoff within next 24 hours
    now     = datetime.now(timezone.utc)
    cutoff  = now + timedelta(hours=24)
    valid: list[dict] = []
    for m in markets:
        kickoff_str = m.get("kickoff", "")
        if not kickoff_str:
            continue
        try:
            kickoff = datetime.fromisoformat(kickoff_str)
            if kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if now <= kickoff <= cutoff:
            valid.append(m)

    log.info("[Matching] %d markets in 24h window", len(valid))

    # Deduplicate: one entry per game
    # Use event_id when non-zero, otherwise (description, kickoff) as key
    seen_games: dict[tuple, dict] = {}
    for m in valid:
        eid = m.get("event_id", 0)
        key: tuple = (eid,) if eid != 0 else (m["description"], m["kickoff"])
        if key not in seen_games:
            seen_games[key] = m

    log.info("[Matching] %d unique Winner games to match", len(seen_games))

    matched: list[MatchedGame] = []
    seen_pinnacle: set[str]    = set()

    for market in seen_games.values():
        sport_id = market["sport_id"]
        group    = SPORT_ID_TO_GROUP.get(sport_id)  # None → search all groups

        home_raw, away_raw = _parse_teams(market["description"])
        if not home_raw or not away_raw:
            log.warning("[Matching] Cannot parse teams from: %r", market["description"])
            continue

        try:
            kickoff = datetime.fromisoformat(market["kickoff"])
            if kickoff.tzinfo is None:
                kickoff = kickoff.replace(tzinfo=timezone.utc)
        except (ValueError, KeyError):
            log.warning("[Matching] Bad kickoff for: %r", market["description"])
            continue

        # Find Pinnacle events in the same sport group within ±15 min of kickoff
        candidates = db.get_by_time(group, kickoff, window_s=_TIME_WINDOW_S)

        if not candidates:
            log.info("[Matching] No Pinnacle coverage: %-28s | %s / %s",
                     market.get("league", ""), home_raw, away_raw)
            continue

        pinnacle_event = _llm_disambiguate(home_raw, away_raw, candidates)
        if pinnacle_event is None:
            log.info(
                "[Matching] No LLM match: %s / %s | candidates: %s",
                home_raw, away_raw,
                [f"{c['home_team']} vs {c['away_team']}" for c in candidates],
            )
            continue

        pinnacle_id = pinnacle_event["event_id"]
        if pinnacle_id in seen_pinnacle:
            continue
        seen_pinnacle.add(pinnacle_id)

        matched.append(MatchedGame(
            winner_event_id   = market.get("event_id", 0),
            pinnacle_event_id = pinnacle_id,
            sport_key         = pinnacle_event["sport_key"],
            pinnacle_home     = pinnacle_event["home_team"],
            pinnacle_away     = pinnacle_event["away_team"],
            winner_home_raw   = home_raw,
            winner_away_raw   = away_raw,
            commence_time     = pinnacle_event["commence_time"],
        ))

    log.info(
        "[Matching] Matched %d / %d unique Winner games",
        len(matched), len(seen_games),
    )
    return matched


# ── Internal helpers ──────────────────────────────────────────────────────────

def _parse_teams(description: str) -> tuple[str, str]:
    """
    Split a Winner description into (home, away).
    Strips Unicode RTL/LTR directional marks that Winner occasionally injects.
    Returns ("", "") if the separator is not found.
    """
    for mark in ("\u202B", "\u202C", "\u200F", "\u200E", "\u202A"):
        description = description.replace(mark, "")
    description = description.strip()

    if " - " not in description:
        return "", ""

    parts = description.split(" - ", 1)
    return parts[0].strip(), parts[1].strip()


def _llm_disambiguate(
    home_raw: str, away_raw: str, candidates: list[dict]
) -> dict | None:
    """
    Ask Claude Haiku which of the candidate Pinnacle events matches the Hebrew game.
    candidates is a list of home-role metadata dicts (one per Pinnacle event).
    Returns the matching dict or None.
    """
    import anthropic

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("[LLM] ANTHROPIC_API_KEY not set.")
        return None

    candidate_lines = "\n".join(
        f"{i + 1}. {c['home_team']} vs {c['away_team']}"
        for i, c in enumerate(candidates)
    )
    prompt = (
        f"Hebrew game: '{home_raw} - {away_raw}'\n\n"
        f"Which of these English matches is the same game?\n\n"
        f"{candidate_lines}\n\n"
        f"Reply with just the number (1–{len(candidates)}) or NONE."
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg    = client.messages.create(
            model      = "claude-haiku-4-5-20251001",
            max_tokens = 10,
            messages   = [{"role": "user", "content": prompt}],
        )
        reply = msg.content[0].text.strip()
        if reply.isdigit():
            idx = int(reply) - 1
            if 0 <= idx < len(candidates):
                return candidates[idx]
    except Exception as exc:
        log.error("[LLM] Disambiguate error: %s", exc)

    return None

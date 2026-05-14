"""
daily_prep_agent.py — Daily preparation agent for the EV betting bot.
Runs at 11:55 Israel time to verify leagues and team translations before the 12:00 run.
"""
import json
import logging
import os
import pathlib

import anthropic
import requests
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

_TRANSLATIONS        = pathlib.Path(__file__).parent.parent / "translations.json"
_MAX_ITERATIONS      = 30
_winner_markets_cache: list[dict] = []   # populated by _fetch_winner_markets


# ── translations.json helpers ─────────────────────────────────────────────────

def _load_translations() -> dict:
    with _TRANSLATIONS.open(encoding="utf-8") as f:
        return json.load(f)


def _save_translations(data: dict) -> None:
    """Atomic write via temp-file rename."""
    tmp = _TRANSLATIONS.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(_TRANSLATIONS)


# ── Tool implementations ───────────────────────────────────────────────────────

async def _fetch_winner_markets(hours: int = 24) -> dict:
    """
    Fetch today's Winner football markets and cross-reference against the
    current translations, so Claude receives both live data and gap analysis
    in one call.
    """
    from datetime import datetime, timedelta, timezone
    import winner_scraper as _ws

    def _wide_filter(markets):
        now    = datetime.now(tz=timezone.utc)
        cutoff = now + timedelta(hours=hours)
        return [
            m for m in markets
            if m["sport_id"] == 240
            and m.get("kickoff")
            and now <= datetime.fromisoformat(m["kickoff"]) <= cutoff
        ]

    async with _ws.WinnerScraper(headless=True) as s:
        result = await s.get_all_markets()

    if "error" in result:
        return {"error": result["error"], "leagues": [], "teams_by_league": {}}

    markets = _wide_filter(result["markets"])

    global _winner_markets_cache
    _winner_markets_cache = markets

    # Build teams-by-league from match descriptions ("HomeTeam - AwayTeam")
    teams_by_league: dict[str, set] = {}
    for m in markets:
        league = m["league"]
        if league not in teams_by_league:
            teams_by_league[league] = set()
        parts = m["description"].split(" - ", 1)
        if len(parts) == 2:
            teams_by_league[league].add(parts[0].strip())
            teams_by_league[league].add(parts[1].strip())

    # Cross-reference against current translations to surface gaps
    data       = _load_translations()
    league_map = data.get("winner_league_to_sport_key", {})
    team_cache = data.get("team_name_cache", {})

    leagues_sorted  = sorted(teams_by_league.keys())
    league_status: dict[str, str] = {}
    teams_needing_translation: dict[str, list[str]] = {}

    for league in leagues_sorted:
        mapping = league_map.get(league, "NOT_MAPPED")
        if mapping is None:
            league_status[league] = "EXPLICITLY_NULL (not on Pinnacle)"
        elif mapping == "NOT_MAPPED":
            league_status[league] = "NOT_MAPPED"
        else:
            league_status[league] = mapping  # the sport_key
            uncached = [t for t in sorted(teams_by_league[league]) if t not in team_cache]
            if uncached:
                teams_needing_translation[league] = uncached

    return {
        "total_markets":              len(markets),
        "leagues":                    leagues_sorted,
        "league_status":              league_status,
        "teams_needing_translation":  teams_needing_translation,
        "teams_by_league":            {k: sorted(v) for k, v in teams_by_league.items()},
    }


def _get_pinnacle_leagues() -> list[dict]:
    api_key = os.getenv("ODDS_API_KEY")
    if not api_key:
        return [{"error": "ODDS_API_KEY not set"}]
    try:
        resp = requests.get(
            "https://api.the-odds-api.com/v4/sports",
            params={"apiKey": api_key, "all": "false"},
            timeout=10,
        )
        resp.raise_for_status()
        return [
            {"key": s["key"], "title": s["title"]}
            for s in resp.json()
            if s.get("group") == "Soccer"
        ]
    except Exception as exc:
        return [{"error": str(exc)}]


def _get_pinnacle_teams(sport_key: str) -> list[str]:
    from pinnacle_client import get_pinnacle_odds
    markets = get_pinnacle_odds([sport_key])
    teams: set[str] = set()
    for m in markets:
        teams.add(m["home_team"])
        teams.add(m["away_team"])
    return sorted(teams)


def _get_current_translation(heb_name: str) -> str:
    data = _load_translations()
    return data.get("team_name_cache", {}).get(heb_name, "NOT_CACHED")


def _save_translation(heb_name: str, eng_name: str) -> str:
    data = _load_translations()
    data.setdefault("team_name_cache", {})[heb_name] = eng_name
    _save_translations(data)
    log.info("[Agent] Saved translation: %r → %r", heb_name, eng_name)
    return f"Saved: {heb_name!r} → {eng_name!r}"


def _save_league_mapping(heb_league: str, sport_key: str | None) -> str:
    data = _load_translations()
    data.setdefault("winner_league_to_sport_key", {})[heb_league] = sport_key
    _save_translations(data)
    # Keep the in-memory constant in sync — it is loaded at import time in pinnacle_client
    import pinnacle_client
    pinnacle_client.WINNER_LEAGUE_MAP[heb_league] = sport_key
    log.info("[Agent] Saved league mapping: %r → %r", heb_league, sport_key)
    return f"Saved: {heb_league!r} → {sport_key!r}"


def _run_match_check() -> dict:
    """
    Run match_markets() against the cached Winner markets and fresh Pinnacle data.
    Uses a 24-hour Pinnacle window so today's full slate is visible.
    """
    if not _winner_markets_cache:
        return {"error": "No Winner markets cached — call fetch_winner_markets first"}

    import matcher as _matcher
    from pinnacle_client import get_pinnacle_odds

    orig_window = _matcher._PINNACLE_WINDOW_MIN
    _matcher._PINNACLE_WINDOW_MIN = 1440
    try:
        data       = _load_translations()
        league_map = data.get("winner_league_to_sport_key", {})
        sport_keys = sorted({v for v in league_map.values() if v})

        pinnacle_markets = get_pinnacle_odds(sport_keys)

        from matcher import match_markets
        pairs = match_markets(_winner_markets_cache, pinnacle_markets)
    finally:
        _matcher._PINNACLE_WINDOW_MIN = orig_window

    # Count total unique 1X2 + תוצאת סיום Winner games
    seen: set[int] = set()
    total = 0
    skipped = 0
    for m in _winner_markets_cache:
        mt = m.get("market_type", "")
        if "1X2" not in mt or "תוצאת סיום" not in mt:
            continue
        if m["event_id"] in seen:
            continue
        seen.add(m["event_id"])
        total += 1
        if m["league"] not in league_map or league_map[m["league"]] is None:
            skipped += 1

    matched = len(pairs)
    unmatched = total - matched - skipped

    by_sport_key: dict[str, int] = {}
    for p in pairs:
        sk = p["pinnacle"]["sport_key"]
        by_sport_key[sk] = by_sport_key.get(sk, 0) + 1

    return {
        "total_winner_games":   total,
        "matched":              matched,
        "skipped_unmapped":     skipped,
        "unmatched_mapped":     unmatched,
        "matched_by_sport_key": dict(sorted(by_sport_key.items(), key=lambda x: -x[1])),
    }


async def _send_telegram(message: str) -> str:
    token   = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return "ERROR: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set"
    from telegram import Bot
    from telegram.error import TelegramError
    bot = Bot(token=token)
    try:
        await bot.send_message(chat_id=chat_id, text=message)
        log.info("[Agent] Telegram summary sent")
        return "Telegram message sent"
    except TelegramError as exc:
        return f"Telegram error: {exc}"


# ── Tool dispatch ──────────────────────────────────────────────────────────────

async def _dispatch(name: str, args: dict) -> str:
    try:
        if name == "fetch_winner_markets":
            return json.dumps(await _fetch_winner_markets(**args), ensure_ascii=False)
        if name == "get_pinnacle_leagues":
            return json.dumps(_get_pinnacle_leagues(), ensure_ascii=False)
        if name == "get_pinnacle_teams":
            return json.dumps(_get_pinnacle_teams(**args), ensure_ascii=False)
        if name == "get_current_translation":
            return _get_current_translation(**args)
        if name == "save_translation":
            return _save_translation(**args)
        if name == "save_league_mapping":
            return _save_league_mapping(**args)
        if name == "run_match_check":
            return json.dumps(_run_match_check(), ensure_ascii=False)
        if name == "send_telegram":
            return await _send_telegram(**args)
        return f"Unknown tool: {name}"
    except Exception as exc:
        log.error("[Agent] Tool %r failed: %s", name, exc)
        return f"Error in {name}: {exc}"


# ── Claude tool definitions ────────────────────────────────────────────────────

_TOOLS = [
    {
        "name": "fetch_winner_markets",
        "description": (
            "Fetch today's Winner.co.il football markets and cross-reference against current translations. "
            "Returns: total_markets, leagues, league_status (each league mapped to its sport_key or 'NOT_MAPPED'), "
            "teams_needing_translation (teams in mapped leagues with no cached translation), and teams_by_league. "
            "Start here — this gives you the full picture of what needs attention in one call."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {
                    "type": "integer",
                    "description": "Look-ahead window in hours. Default 24.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_pinnacle_leagues",
        "description": (
            "List all soccer leagues currently available on Pinnacle via The Odds API. "
            "Use this to find the correct sport_key for a Winner league that has no mapping."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_pinnacle_teams",
        "description": (
            "Get all team names currently active in a Pinnacle league. "
            "Use this to find the correct English name to match a Hebrew team from Winner."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sport_key": {
                    "type": "string",
                    "description": "Odds API sport key, e.g. 'soccer_epl'",
                },
            },
            "required": ["sport_key"],
        },
    },
    {
        "name": "get_current_translation",
        "description": "Check the cached English translation for a Hebrew team name. Returns the cached value or 'NOT_CACHED'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "heb_name": {
                    "type": "string",
                    "description": "Hebrew team name as it appears on Winner",
                },
            },
            "required": ["heb_name"],
        },
    },
    {
        "name": "save_translation",
        "description": "Persist a Hebrew→English team name translation to translations.json.",
        "input_schema": {
            "type": "object",
            "properties": {
                "heb_name": {
                    "type": "string",
                    "description": "Hebrew team name as it appears on Winner",
                },
                "eng_name": {
                    "type": "string",
                    "description": "Exact English team name as it appears on Pinnacle",
                },
            },
            "required": ["heb_name", "eng_name"],
        },
    },
    {
        "name": "save_league_mapping",
        "description": (
            "Persist a Hebrew league → Pinnacle sport_key mapping to translations.json. "
            "Pass sport_key as null if the league is not available on Pinnacle."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "heb_league": {
                    "type": "string",
                    "description": "Hebrew league name as it appears on Winner",
                },
                "sport_key": {
                    "type": ["string", "null"],
                    "description": "Odds API sport key, or null if not on Pinnacle",
                },
            },
            "required": ["heb_league", "sport_key"],
        },
    },
    {
        "name": "run_match_check",
        "description": (
            "Run match_markets() against today's Winner data and live Pinnacle odds to measure coverage. "
            "Returns total_winner_games, matched, skipped_unmapped, unmatched_mapped, and matched_by_sport_key. "
            "Call this after all league mappings and team translations are fixed, before send_telegram()."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "send_telegram",
        "description": (
            "Send the final preparation summary to the Telegram channel. "
            "Call this as your last action once all verifications and the match check are complete."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": (
                        "Full report including: leagues checked/fixed, translations checked/fixed, "
                        "today's coverage stats from run_match_check(), and any unresolved issues"
                    ),
                },
            },
            "required": ["message"],
        },
    },
]


# ── System prompt ──────────────────────────────────────────────────────────────

_SYSTEM = """\
You are a preparation agent for an EV betting bot that finds positive expected-value opportunities \
by comparing Winner.co.il (Israel) odds against Pinnacle sharp odds.

The bot runs every 10 minutes starting at 12:00 Israel time. Your job is to ensure it is ready \
by verifying all data mappings are correct before it starts.

== Data structures ==

translations.json["winner_league_to_sport_key"]
  Maps Hebrew league names (as they appear on Winner) to Odds API sport keys (e.g. "soccer_epl").
  A league mapped to null is explicitly known to be unavailable on Pinnacle.

translations.json["team_name_cache"]
  Maps Hebrew team names to exact English team names as they appear on Pinnacle.
  The bot uses these to match Winner games to Pinnacle odds.

== Definition of "ready" ==
1. Every league in today's Winner markets either:
   a) has a sport_key mapping, OR
   b) is explicitly mapped to null (not available on Pinnacle)
2. Every team in a mapped (non-null) league has a translation in team_name_cache.

== Workflow ==
1. Call fetch_winner_markets() — it returns both the live data AND the current translation status, \
so you can see immediately which leagues are unmapped and which teams need translation.
2. For each league with status NOT_MAPPED: call get_pinnacle_leagues() to find the correct sport_key \
and save it with save_league_mapping(). If it is a niche league not on Pinnacle, save it with null.
3. For each team listed in teams_needing_translation: call get_pinnacle_teams(sport_key) for that \
league to get the candidate list, identify the correct English match, and save it with save_translation().
4. Call run_match_check() to get today's coverage statistics.
5. Call send_telegram() with the full report including coverage stats and stop.

Be efficient. Skip teams already cached. If a league or team cannot be resolved, note it in the summary \
and move on. When everything fixable is fixed, send the summary and stop."""


# ── Agent entrypoint ───────────────────────────────────────────────────────────

async def run_agent() -> None:
    """Run the preparation agent. Blocks until Claude emits end_turn or max iterations."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        log.error("[Agent] ANTHROPIC_API_KEY not set — aborting")
        return

    client   = anthropic.Anthropic(api_key=api_key)
    messages = [{"role": "user", "content": "Run the preparation check for today."}]

    log.info("[Agent] Preparation agent starting")

    for iteration in range(1, _MAX_ITERATIONS + 1):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
            tools=_TOOLS,
            messages=messages,
        )

        log.info("[Agent] Turn %d — stop_reason=%s", iteration, response.stop_reason)

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            log.info("[Agent] Preparation complete after %d turn(s)", iteration)
            break

        if response.stop_reason != "tool_use":
            log.warning("[Agent] Unexpected stop_reason %r — stopping", response.stop_reason)
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            log.info("[Agent] → %s(%s)", block.name,
                     json.dumps(block.input, ensure_ascii=False)[:120])
            result_str = await _dispatch(block.name, block.input)
            log.info("[Agent] ← %s", result_str[:200])
            tool_results.append({
                "type":        "tool_result",
                "tool_use_id": block.id,
                "content":     result_str,
            })

        messages.append({"role": "user", "content": tool_results})

    else:
        log.warning("[Agent] Reached max iterations (%d) without end_turn", _MAX_ITERATIONS)

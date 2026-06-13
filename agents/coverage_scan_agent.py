"""
coverage_scan_agent.py — Daily multi-sport coverage scan.

Finds Winner↔Pinnacle game matches across Soccer, Basketball, Tennis, and Baseball
without a pre-built league map. Runs once daily at 08:00 IL.

Two-phase approach per sport:
  1. Agent reasons about which Winner Hebrew leagues map to Pinnacle sport keys.
  2. For each matched league pair, Haiku matches individual games by Hebrew description.

Output: data/coverage_{date}.json — flat list of {winner_event_id, pinnacle_id, kickoff}
Zero Odds API quota cost (uses free /sports and /events endpoints only).
"""
import asyncio
import json
import logging
import os
import pathlib
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Ensure project root is on the path when running this file directly
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import anthropic
import requests
from dotenv import load_dotenv
from telegram_bot import send_coverage_report

load_dotenv()

log = logging.getLogger(__name__)

_TRANSLATIONS    = pathlib.Path(__file__).parent.parent / "translations.json"
_DATA_DIR        = pathlib.Path(__file__).parent.parent / "data"
_SOCCER_SPORT_ID = 240
_SCAN_HOURS      = 36
_KICKOFF_DELTA   = timedelta(minutes=15)
_MAX_ITERATIONS  = 20

_SPORT_GROUPS = {
    "soccer":     "Soccer",
    "basketball": "Basketball",
    "tennis":     "Tennis",
    "baseball":   "Baseball",
}

# Written by orchestrator before sport agents start; each agent reads only its own key.
_winner_games_cache: dict[str, dict[str, list[dict]]] = {}

_IL_TZ = ZoneInfo("Asia/Jerusalem")


def _to_il(iso_str: str) -> str:
    """Convert any ISO datetime string to Israel local time."""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone(_IL_TZ).isoformat()
    except (ValueError, AttributeError):
        return iso_str


def _is_game(description: str) -> bool:
    """
    Returns False for outright/special Winner markets that are not real games.
    Real games are always 'Team A - Team B'. Outrights either have no ' - '
    or contain a 4-digit year on one side (e.g. 'הזוכה בבית C - מונדיאל 2026').
    """
    if " - " not in description:
        return False
    parts = description.split(" - ", 1)
    return not any(re.search(r"\d{4}", p) for p in parts)


# ── translations.json helpers ─────────────────────────────────────────────────

def _load_translations() -> dict:
    with _TRANSLATIONS.open(encoding="utf-8") as f:
        return json.load(f)


def _save_translations(data: dict) -> None:
    tmp = _TRANSLATIONS.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(_TRANSLATIONS)


# ── Phase 0: sport_id bootstrap ───────────────────────────────────────────────

async def _discover_winner_sport_ids() -> dict:
    """
    Run the Winner scraper once with no sport filter, classify unknown sport_ids
    via a single Haiku call, save to translations.json["winner_sport_ids"].
    Only runs when the cache is absent or incomplete.
    """
    from winner_scraper import WinnerScraper

    log.info("[Bootstrap] Fetching all Winner markets for sport_id discovery...")
    async with WinnerScraper(headless=True, sport_ids=None) as s:
        result = await s.get_all_markets()

    if "error" in result:
        raise RuntimeError(f"Scraper failed during bootstrap: {result['error']}")

    # Collect up to 5 sample Hebrew descriptions per sport_id (skip known soccer=240)
    by_sport_id: dict[int, list[str]] = {}
    for m in result["markets"]:
        sid = m["sport_id"]
        if sid == _SOCCER_SPORT_ID:
            continue
        by_sport_id.setdefault(sid, [])
        if len(by_sport_id[sid]) < 5:
            by_sport_id[sid].append(m["description"])

    if not by_sport_id:
        log.warning("[Bootstrap] No non-soccer sport_ids found — nothing to classify")
        return {}

    sample_text = "\n".join(
        f'sport_id {sid}: {descs}' for sid, descs in by_sport_id.items()
    )
    prompt = (
        "Below are Winner.co.il sport_ids with sample Hebrew match descriptions.\n"
        "Classify each sport_id as exactly one of: soccer, basketball, tennis, baseball, other.\n"
        'Respond with ONLY valid JSON like: {"241": "basketball", "244": "tennis"}\n\n'
        + sample_text
    )

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()

    try:
        classified = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            raise ValueError(f"Could not parse Bootstrap Haiku response: {raw}")
        classified = json.loads(m.group())

    target = {"basketball", "tennis", "baseball"}
    sport_id_map = {
        sport: int(sid_str)
        for sid_str, sport in classified.items()
        if sport in target
    }
    log.info("[Bootstrap] Discovered sport_ids: %s", sport_id_map)

    data = _load_translations()
    data["winner_sport_ids"] = sport_id_map
    _save_translations(data)
    return sport_id_map


# ── Winner data fetch ─────────────────────────────────────────────────────────

async def _fetch_winner_games(sport_id_map: dict) -> dict[str, list[dict]]:
    """Single scraper run — splits returned markets by sport name."""
    from winner_scraper import WinnerScraper

    id_to_sport = {
        _SOCCER_SPORT_ID: "soccer",
        **{v: k for k, v in sport_id_map.items() if v is not None},
    }

    log.info("[CoverageScan] Fetching Winner markets (all sports)...")
    async with WinnerScraper(headless=True, sport_ids=None) as s:
        result = await s.get_all_markets()

    if "error" in result:
        log.error("[CoverageScan] Winner fetch failed: %s", result["error"])
        return {sport: [] for sport in _SPORT_GROUPS}

    now    = datetime.now(tz=timezone.utc)
    cutoff = now + timedelta(hours=_SCAN_HOURS)

    games_by_sport: dict[str, list[dict]] = {sport: [] for sport in _SPORT_GROUPS}

    for m in result["markets"]:
        sport_name = id_to_sport.get(m["sport_id"])
        if sport_name is None:
            continue
        try:
            kickoff = datetime.fromisoformat(m["kickoff"])
        except (ValueError, KeyError):
            continue
        if now <= kickoff <= cutoff:
            games_by_sport[sport_name].append(m)

    for sport, games in games_by_sport.items():
        log.info("[CoverageScan] Winner %s: %d games in next %dh", sport, len(games), _SCAN_HOURS)

    return games_by_sport


# ── Pinnacle API helpers (sync — called from threads) ─────────────────────────

def _fetch_pinnacle_events_for_group(sport_group: str) -> dict[str, dict]:
    """
    Fetch all active Pinnacle sport keys + events for a sport group.
    Uses only free endpoints — zero quota cost.

    Returns:
        {sport_key: {"title": str, "events": [{id, home_team, away_team, commence_time}]}}
    """
    api_key = os.getenv("ODDS_API_KEY")
    now     = datetime.now(tz=timezone.utc)
    cutoff  = now + timedelta(hours=_SCAN_HOURS)

    resp = requests.get(
        "https://api.the-odds-api.com/v4/sports",
        params={"apiKey": api_key},
        timeout=10,
    )
    resp.raise_for_status()

    keys_with_titles = {
        s["key"]: s["title"]
        for s in resp.json()
        if s.get("group") == sport_group
        and s.get("active")
        and not s.get("has_outrights")
    }
    log.info("[CoverageScan] %s: %d active Pinnacle keys", sport_group, len(keys_with_titles))

    result = {}
    for key, title in keys_with_titles.items():
        try:
            r = requests.get(
                f"https://api.the-odds-api.com/v4/sports/{key}/events",
                params={
                    "apiKey":           api_key,
                    "commenceTimeFrom": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "commenceTimeTo":   cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
                timeout=10,
            )
            if r.status_code == 422:
                continue
            r.raise_for_status()
            events = r.json()
            if events:
                result[key] = {
                    "title":  title,
                    "events": [
                        {
                            "id":            e["id"],
                            "home_team":     e["home_team"],
                            "away_team":     e["away_team"],
                            "commence_time": e["commence_time"],
                        }
                        for e in events
                    ],
                }
        except requests.RequestException as exc:
            log.warning("[CoverageScan] Events fetch failed for %s: %s", key, exc)

    log.info("[CoverageScan] %s: %d keys with upcoming events", sport_group, len(result))
    return result


def _match_single_team(
    client: anthropic.Anthropic,
    hebrew_name: str,
    candidates: list[dict],
    side: str,
) -> str:
    """Single-team fallback: match on home or away name alone when full-description fails."""
    team_key = "home_team" if side == "home" else "away_team"
    candidates_text = "\n".join(
        f'{c["id"]}  {c[team_key]}'
        for c in candidates
    )
    prompt = (
        f"Return ONLY the 32-character hex id of the candidate whose {side} team matches the Hebrew name, or NO_MATCH. No explanation.\n\n"
        f'Hebrew {side} team name: "{hebrew_name}"\n\n'
        f"Candidates (id  {side}_team):\n{candidates_text}\n\n"
        "Notes: Hebrew name is a phonetic transliteration of English. "
        "Dotted abbreviations like ה.י.ק. = HJK (initials)."
    )
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
    except Exception as exc:
        log.error("[CoverageScan] Haiku single-team fallback error for %r: %s", hebrew_name, exc)
        return "NO_MATCH"

    hex_match = re.search(r'\b([0-9a-f]{32})\b', raw)
    if hex_match:
        return hex_match.group(1)
    return "NO_MATCH"


def _match_games_in_league(
    winner_games: list[dict],
    pinnacle_events: list[dict],
    league: str,
    pinnacle_league: str = "",
) -> tuple[list[dict], list[dict]]:
    """
    For each Winner game, filter Pinnacle events to ±15 min candidates and ask
    Haiku to identify the match by Hebrew description. No splitting, no per-team
    translation — Haiku reasons about the whole description at once.

    Returns (matched_pairs, unmatched_games). Unmatched games include the reason
    ("no_candidates" or "no_match") and, for no_match, the candidates Haiku saw.
    """
    # Deduplicate by event_id — Winner has multiple markets per event (1X2, totals, etc.)
    seen_event_ids: set[int] = set()
    deduped: list[dict] = []
    for wm in winner_games:
        if wm["event_id"] not in seen_event_ids:
            seen_event_ids.add(wm["event_id"])
            deduped.append(wm)

    if not deduped:
        return [], []

    if not pinnacle_events:
        return [], [
            {
                "winner_event_id": wm["event_id"],
                "description":     wm.get("description", ""),
                "league":          league,
                "pinnacle_league": pinnacle_league,
                "kickoff":         _to_il(wm.get("kickoff", "")),
                "reason":          "no_candidates",
            }
            for wm in deduped
        ]

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    pairs: list[dict] = []
    unmatched: list[dict] = []

    for wm in deduped:
        try:
            winner_ko = datetime.fromisoformat(wm["kickoff"])
        except (ValueError, KeyError):
            continue

        candidates = [
            pe for pe in pinnacle_events
            if abs(
                winner_ko
                - datetime.fromisoformat(pe["commence_time"].replace("Z", "+00:00"))
            ) <= _KICKOFF_DELTA
        ]
        if not candidates:
            unmatched.append({
                "winner_event_id": wm["event_id"],
                "description":     wm.get("description", ""),
                "league":          league,
                "pinnacle_league": pinnacle_league,
                "kickoff":         _to_il(wm.get("kickoff", "")),
                "reason":          "no_candidates",
            })
            continue

        candidates_text = "\n".join(
            f'{c["id"]}  {c["home_team"]} vs {c["away_team"]}  {c["commence_time"]}'
            for c in candidates
        )
        prompt = (
            "Return ONLY the 32-character hex id of the matching candidate, or NO_MATCH. No explanation.\n\n"
            f'Winner Hebrew description: "{wm["description"]}"\n\n'
            f"Pinnacle candidates (id  home vs away  kickoff):\n{candidates_text}\n\n"
            "Notes: Hebrew names are phonetic transliterations of English. "
            "Dotted abbreviations like ה.י.ק. = HJK (initials). "
            "The Hebrew format is always 'home team - away team' — match first name to home, second to away. "
            "Single strong candidate: prefer returning the id over NO_MATCH."
        )

        try:
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = resp.content[0].text.strip()
        except Exception as exc:
            log.error("[CoverageScan] Haiku game-match error for %r: %s", wm["description"], exc)
            continue

        # Haiku sometimes adds explanation after the id — extract the 32-char hex ID if present
        hex_match = re.search(r'\b([0-9a-f]{32})\b', raw)
        if hex_match:
            returned_id = hex_match.group(1)
        elif "NO_MATCH" in raw:
            returned_id = "NO_MATCH"
        else:
            returned_id = raw

        il_candidates = [
            {**c, "commence_time": _to_il(c["commence_time"])}
            for c in candidates
        ]

        if returned_id == "NO_MATCH":
            description = wm.get("description", "")
            parts = description.split(" - ")
            if len(parts) == 2:
                home_heb, away_heb = parts[0].strip(), parts[1].strip()
                for side, heb in [("home", home_heb), ("away", away_heb)]:
                    fallback_id = _match_single_team(client, heb, candidates, side)
                    if next((c for c in candidates if c["id"] == fallback_id), None):
                        log.info(
                            "[CoverageScan] Single-team fallback (%s): %r → matched",
                            side, description,
                        )
                        returned_id = fallback_id
                        break

        if returned_id == "NO_MATCH":
            unmatched.append({
                "winner_event_id": wm["event_id"],
                "description":     wm.get("description", ""),
                "league":          league,
                "pinnacle_league": pinnacle_league,
                "kickoff":         _to_il(wm.get("kickoff", "")),
                "reason":          "no_match",
                "candidates":      il_candidates,
            })
            continue

        matched = next((c for c in candidates if c["id"] == returned_id), None)
        if matched is None:
            log.warning(
                "[CoverageScan] Haiku returned unknown id %r for %r — skipping",
                returned_id, wm["description"],
            )
            unmatched.append({
                "winner_event_id": wm["event_id"],
                "description":     wm.get("description", ""),
                "league":          league,
                "pinnacle_league": pinnacle_league,
                "kickoff":         _to_il(wm.get("kickoff", "")),
                "reason":          "no_match",
                "candidates":      il_candidates,
            })
            continue

        log.info(
            "[CoverageScan] Matched: %r → %s vs %s",
            wm["description"], matched["home_team"], matched["away_team"],
        )
        pairs.append({
            "winner_event_id":    wm["event_id"],
            "pinnacle_id":        returned_id,
            "kickoff":            _to_il(wm["kickoff"]),
            "winner_description": wm.get("description", ""),
            "pinnacle_name":      f"{matched['home_team']} vs {matched['away_team']}",
        })

    return pairs, unmatched


# ── Sport agent (sync — runs in thread) ───────────────────────────────────────

def _run_sport_agent_sync(
    sport_name: str,
    sport_group: str,
    winner_games: list[dict],
) -> dict:
    """
    Claude Haiku agent for one sport. Phase 1: agent matches leagues via reasoning.
    Phase 2: tool call matches individual games via Haiku.

    Returns {"pairs": [...], "orphan_leagues": [...], "orphan_games": [...]}.
    """
    if not winner_games:
        log.info("[CoverageScan] %s: 0 Winner games — skipping", sport_name)
        return {"pairs": [], "orphan_leagues": [], "orphan_games": []}

    # Group winner games by league and store in module-level cache
    by_league: dict[str, list[dict]] = {}
    for g in winner_games:
        league = g.get("league") or "unknown"
        by_league.setdefault(league, []).append(g)
    _winner_games_cache[sport_name] = by_league

    winner_league_lines = "\n".join(
        f"  - {league} ({len(games)} game{'s' if len(games) != 1 else ''})"
        for league, games in sorted(by_league.items())
    )

    # Pinnacle events fetched on first tool call; stored here for subsequent calls
    pinnacle_cache: dict[str, list[dict]] = {}
    pinnacle_title_cache: dict[str, str] = {}
    collected_pairs: list[dict] = []
    matched_league_names: set[str] = set()
    orphan_games: list[dict] = []
    already_retried: set[str] = set()  # winner leagues that already got a wrong-key warning

    tools = [
        {
            "name": "fetch_pinnacle_events",
            "description": (
                f"Fetch all upcoming {sport_group} competitions from Pinnacle. "
                "Returns each sport key with its title, event count, and sample team names "
                "so you can decide which Winner leagues correspond to which Pinnacle keys."
            ),
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "name": "match_games_in_league",
            "description": (
                "Match Winner games for one Hebrew league to Pinnacle events for one sport key. "
                "If the response contains warning=all_no_candidates, the Pinnacle key was wrong — "
                "call again with a different pinnacle_sport_key for the same winner_league."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "winner_league": {
                        "type": "string",
                        "description": "Hebrew Winner league name exactly as listed",
                    },
                    "pinnacle_sport_key": {
                        "type": "string",
                        "description": "Pinnacle sport key e.g. 'basketball_nba'",
                    },
                },
                "required": ["winner_league", "pinnacle_sport_key"],
            },
        },
        {
            "name": "return_results",
            "description": "Signal that all leagues have been processed. Call once at the very end.",
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
    ]

    system_prompt = (
        f"You are a sports matching agent for {sport_name}.\n\n"
        f"Your job:\n"
        f"1. Call fetch_pinnacle_events — returns active Pinnacle {sport_group} competitions "
        f"with titles and sample team names.\n"
        f"2. For each Winner league below that clearly corresponds to a Pinnacle sport key "
        f"(same competition, same sport), call match_games_in_league.\n"
        f"3. When all leagues are processed, call return_results.\n\n"
        f"Winner {sport_name} leagues today:\n{winner_league_lines}\n\n"
        f"Rules:\n"
        f"- Only match within {sport_group} — never cross sports\n"
        f"- Skip Winner leagues with no clear Pinnacle equivalent\n"
        f"- If match_games_in_league returns warning=all_no_candidates, the Pinnacle key was wrong: "
        f"try a different key for that winner league. If no better key exists, skip it.\n"
        f"- Call return_results exactly once at the end"
    )

    messages = [
        {"role": "user", "content": f"Match {sport_name} games. Start by calling fetch_pinnacle_events."}
    ]
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    for _ in range(_MAX_ITERATIONS):
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4096,
            system=system_prompt,
            tools=tools,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            break

        tool_results = []
        done = False

        for block in response.content:
            if block.type != "tool_use":
                continue

            try:
                if block.name == "fetch_pinnacle_events":
                    raw = _fetch_pinnacle_events_for_group(sport_group)
                    pinnacle_cache.update({k: v["events"] for k, v in raw.items()})
                    pinnacle_title_cache.update({k: v["title"] for k, v in raw.items()})
                    # Return a compact summary — agent needs titles + sample teams to match leagues
                    summary = {
                        key: {
                            "title":        info["title"],
                            "event_count":  len(info["events"]),
                            "sample_teams": sorted({
                                t
                                for e in info["events"]
                                for t in (e["home_team"], e["away_team"])
                            })[:8],
                        }
                        for key, info in raw.items()
                    }
                    content = json.dumps(summary)

                elif block.name == "match_games_in_league":
                    winner_league      = block.input["winner_league"]
                    pinnacle_sport_key = block.input["pinnacle_sport_key"]
                    league_games       = _winner_games_cache.get(sport_name, {}).get(winner_league, [])
                    pinnacle_events    = pinnacle_cache.get(pinnacle_sport_key, [])

                    # Soccer only: drop outright/special markets before matching.
                    # Real games are always "Team A - Team B" with no 4-digit year.
                    # Actual World Cup games like "ארגנטינה - צרפת" still pass this filter.
                    if sport_name == "soccer":
                        league_games = [g for g in league_games if _is_game(g.get("description", ""))]

                    pinnacle_title = pinnacle_title_cache.get(pinnacle_sport_key, "")
                    pairs, unmatched = _match_games_in_league(league_games, pinnacle_events, winner_league, pinnacle_title)

                    no_cand_count = sum(1 for g in unmatched if g["reason"] == "no_candidates")
                    # Trigger retry when zero matches and majority of games lack any time-window
                    # candidates — strong signal the Pinnacle key is wrong (not just hard to match).
                    wrong_key = (
                        not pairs and bool(unmatched) and
                        no_cand_count > (len(unmatched) - no_cand_count)
                    )

                    if wrong_key and winner_league not in already_retried:
                        # First attempt: likely wrong Pinnacle key — signal agent to retry
                        already_retried.add(winner_league)
                        log.info(
                            "[CoverageScan] %s / '%s': %d/%d games no_candidates under '%s' — wrong key, signalling retry",
                            sport_name, winner_league, no_cand_count, len(unmatched), pinnacle_sport_key,
                        )
                        content = json.dumps({
                            "matched": 0,
                            "unmatched": len(unmatched),
                            "warning": "all_no_candidates",
                            "message": (
                                f"None of the {len(unmatched)} games in '{winner_league}' found any "
                                f"Pinnacle events in the ±15min window under '{pinnacle_sport_key}'. "
                                "Likely wrong key. Try a different pinnacle_sport_key for this winner league, "
                                "or proceed to return_results if no better key exists."
                            ),
                        })
                        # Don't add to matched_league_names — keep it open for retry
                    else:
                        matched_league_names.add(winner_league)
                        collected_pairs.extend(pairs)
                        if wrong_key:
                            # Second attempt also wrong — treat as orphan league, not orphan games
                            log.info(
                                "[CoverageScan] %s / '%s': still all no_candidates after retry — orphan league",
                                sport_name, winner_league,
                            )
                        else:
                            orphan_games.extend(unmatched)
                        content = json.dumps({"matched": len(pairs), "unmatched": len(unmatched)})

                elif block.name == "return_results":
                    content = json.dumps({"status": "done", "total": len(collected_pairs)})
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     content,
                    })
                    done = True
                    break

                else:
                    content = json.dumps({"error": f"Unknown tool: {block.name}"})

            except Exception as exc:
                log.error("[CoverageScan] %s / tool %s failed: %s", sport_name, block.name, exc)
                content = json.dumps({"error": str(exc)})

            tool_results.append({
                "type":        "tool_result",
                "tool_use_id": block.id,
                "content":     content,
            })

        if tool_results:
            messages.append({"role": "user", "content": tool_results})

        if done:
            break

    orphan_leagues = [
        {
            "league":     league,
            "game_count": len({g["event_id"] for g in games}),
        }
        for league, games in sorted(by_league.items())
        if league not in matched_league_names
    ]

    log.info(
        "[CoverageScan] %s agent done: %d matched pairs, %d orphan leagues, %d orphan games",
        sport_name, len(collected_pairs), len(orphan_leagues), len(orphan_games),
    )
    return {
        "pairs":          collected_pairs,
        "orphan_leagues": orphan_leagues,
        "orphan_games":   orphan_games,
    }


# ── Output ────────────────────────────────────────────────────────────────────

def _write_orphans(report: dict) -> None:
    from datetime import date
    today = date.today().isoformat()
    _DATA_DIR.mkdir(exist_ok=True)
    output_path = _DATA_DIR / f"orphans_{today}.json"
    tmp = output_path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump({"date": today, **report}, f, ensure_ascii=False, indent=2)
    tmp.replace(output_path)
    log.info("[CoverageScan] Orphan report written to %s", output_path)


def _write_output(pairs: list[dict]) -> None:
    from datetime import date
    today = date.today().isoformat()
    _DATA_DIR.mkdir(exist_ok=True)
    output_path = _DATA_DIR / f"coverage_{today}.json"
    tmp = output_path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(pairs, f, ensure_ascii=False, indent=2)
    tmp.replace(output_path)
    log.info("[CoverageScan] Written %d pairs to %s", len(pairs), output_path)


# ── Public entry point ────────────────────────────────────────────────────────

async def run_coverage_scan() -> None:
    """
    Entry point called by scheduler.py at 08:00 IL.
    On first run: bootstraps Winner sport_ids via a Haiku classification call.
    Every run: single scraper fetch → 4 parallel sport agents → JSON output.
    """
    log.info("[CoverageScan] Starting daily coverage scan")

    # 1. Load or bootstrap sport_ids
    data   = _load_translations()
    cached = data.get("winner_sport_ids", {})
    target = {"basketball", "tennis", "baseball"}
    if not target.issubset(cached.keys()):
        log.info("[CoverageScan] winner_sport_ids incomplete — running bootstrap")
        cached = await _discover_winner_sport_ids()

    sport_id_map = {k: cached.get(k) for k in target}

    # 2. Single Winner scraper run
    winner_games_by_sport = await _fetch_winner_games(sport_id_map)

    # 3. Four sport agents in parallel (each runs in its own thread)
    sport_tasks = [
        asyncio.to_thread(
            _run_sport_agent_sync,
            sport_name,
            _SPORT_GROUPS[sport_name],
            winner_games_by_sport.get(sport_name, []),
        )
        for sport_name in _SPORT_GROUPS
    ]
    results = await asyncio.gather(*sport_tasks, return_exceptions=True)

    # 4. Collect results
    all_pairs: list[dict] = []
    orphan_report: dict = {}
    for sport_name, result in zip(_SPORT_GROUPS.keys(), results):
        if isinstance(result, Exception):
            log.error("[CoverageScan] %s agent failed: %s", sport_name, result, exc_info=result)
        else:
            all_pairs.extend(result["pairs"])
            orphan_report[sport_name] = {
                "matched_count":  len(result["pairs"]),
                "pairs":          result["pairs"],
                "orphan_leagues": result["orphan_leagues"],
                "orphan_games":   result["orphan_games"],
            }

    # 5. Write output
    _write_output(all_pairs)
    _write_orphans(orphan_report)
    log.info("[CoverageScan] Complete — %d total matched pairs", len(all_pairs))
    await send_coverage_report(len(all_pairs), orphan_report)


# ── Manual test run ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(run_coverage_scan())

"""
match_odds.py — Manual EV scan for soccer games in today's coverage window.
Run: python match_odds.py
"""
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from database import init_db, save_alert, alert_exists
from ev_calculator import calculate_ev
from pinnacle_client import get_event_odds
from telegram_bot import send_alerts
from winner_scraper import WinnerScraper

load_dotenv()

log = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent / "data"


def _load_coverage() -> list[dict]:
    today = datetime.now(tz=timezone.utc).date().isoformat()
    path = _DATA_DIR / f"coverage_{today}.json"
    if not path.exists():
        log.error("[MatchOdds] No coverage file for %s — run coverage_scan_agent.py first", today)
        return []
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _active_soccer_pairs(coverage: list[dict]) -> list[dict]:
    return [p for p in coverage if p.get("sport_key", "").startswith("soccer")]


def _build_pairs(winner_markets: list[dict], pinn_event: dict, winner_event_id: int) -> list[dict]:
    pairs = []
    event_markets = [m for m in winner_markets if m.get("event_id") == winner_event_id]

    # 1X2 — full-time
    for wm in event_markets:
        mt = wm.get("market_type", "")
        if "1X2" in mt and "תוצאת סיום" in mt:
            pairs.append({"winner": wm, "pinnacle": pinn_event, "market": "h2h"})
            break

    # Over/Under goals — full-time
    for wm in event_markets:
        mt = wm.get("market_type", "")
        if "מעל/מתחת שערים" in mt and "תוצאת סיום" in mt:
            try:
                winner_point = float(wm["outcomes"][0]["desc"].strip("\u202B\u202C").split()[-1])
            except (IndexError, ValueError, KeyError):
                log.warning("[MatchOdds] Could not extract totals point from Winner market")
                break
            matched_line = next((t for t in pinn_event["totals"] if t["point"] == winner_point), None)
            if matched_line:
                pairs.append({
                    "winner": wm,
                    "pinnacle": {
                        **matched_line,
                        "home_team": pinn_event["home_team"],
                        "away_team": pinn_event["away_team"],
                    },
                    "market": "totals",
                })
            else:
                log.info(
                    "[MatchOdds] Totals line mismatch: Winner=%.1f not in Pinnacle %s",
                    winner_point, [t["point"] for t in pinn_event["totals"]],
                )
            break

    # BTTS — full-time
    for wm in event_markets:
        mt = wm.get("market_type", "")
        if "האם כל קבוצה תבקיע" in mt and "תוצאת סיום" in mt:
            pairs.append({"winner": wm, "pinnacle": pinn_event, "market": "btts"})
            break

    return pairs


async def run() -> None:
    init_db()

    coverage = _load_coverage()
    if not coverage:
        return

    active = _active_soccer_pairs(coverage)
    if not active:
        log.info("[MatchOdds] No soccer pairs in today's coverage")
        return

    log.info("[MatchOdds] %d soccer pair(s) from coverage", len(active))

    async with WinnerScraper(headless=True) as scraper:
        result = await scraper.get_all_markets()
    winner_markets = result.get("markets", [])

    all_pairs = []
    for pair in active:
        pinn_event = get_event_odds(pair["sport_key"], pair["pinnacle_id"])
        if pinn_event is None:
            log.warning("[MatchOdds] No Pinnacle odds for %s", pair.get("pinnacle_name"))
            continue

        pairs = _build_pairs(winner_markets, pinn_event, pair["winner_event_id"])
        log.info(
            "[MatchOdds] %s — %d market pair(s) [%s]",
            pair.get("pinnacle_name"),
            len(pairs),
            ", ".join(p["market"] for p in pairs),
        )
        all_pairs.extend(pairs)

    if not all_pairs:
        log.info("[MatchOdds] No pairs to evaluate")
        return

    alerts = calculate_ev(all_pairs)

    new_alerts = []
    for alert in alerts:
        event_id = alert.get("winner_event_id")
        if event_id and not alert_exists(event_id, alert["outcome"], alert["ev_pct"]):
            save_alert(alert, event_id)
            new_alerts.append(alert)
        elif not event_id:
            new_alerts.append(alert)

    log.info("[MatchOdds] %d alert(s) — %d new", len(alerts), len(new_alerts))
    await send_alerts(new_alerts)

    if not alerts:
        log.info("[MatchOdds] No +EV opportunities found")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(run())

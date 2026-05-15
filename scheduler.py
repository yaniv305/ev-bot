"""
scheduler.py — Main loop: runs the full EV pipeline every 10 minutes.
"""
import asyncio
import logging
import sys
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from winner_scraper import WinnerScraper, _filter_upcoming_football
from pinnacle_client import get_pinnacle_odds, WINNER_LEAGUE_MAP
from matcher import match_markets
from ev_calculator import calculate_ev
from telegram_bot import send_alerts
from database import init_db, save_alert, alert_exists, get_pending_results, update_result
from results_fetcher import get_results_batch
from agents.daily_prep_agent import run_agent

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

_ISRAEL_TZ    = ZoneInfo("Asia/Jerusalem")
_RUN_INTERVAL = timedelta(minutes=10)
_ACTIVE_START = 12   # 12:00 Israel
_ACTIVE_END   = 22   # 22:00 Israel
_PINNACLE_TTL = timedelta(minutes=20)

agent_ran_today: date | None = None

# In-memory Pinnacle cache
_pinnacle_cache: list[dict] | None = None
_pinnacle_cache_time: datetime | None = None
_pinnacle_cache_keys: set[str] = set()


def _get_pinnacle_odds_cached(sport_keys: list[str]) -> list[dict]:
    global _pinnacle_cache, _pinnacle_cache_time, _pinnacle_cache_keys
    now = datetime.now(tz=_ISRAEL_TZ)
    current_keys = set(sport_keys)
    new_keys = current_keys - _pinnacle_cache_keys
    if (
        _pinnacle_cache is not None
        and _pinnacle_cache_time is not None
        and now - _pinnacle_cache_time < _PINNACLE_TTL
        and not new_keys
    ):
        log.info("[Pinnacle] Using cached odds")
        return _pinnacle_cache
    if new_keys and _pinnacle_cache is not None:
        log.info("[Pinnacle] New leagues detected — refreshing cache: %s", sorted(new_keys))
    else:
        log.info("[Pinnacle] Fetching fresh odds")
    result = get_pinnacle_odds(sport_keys)
    _pinnacle_cache = result
    _pinnacle_cache_time = now
    _pinnacle_cache_keys = current_keys
    return result


async def _run_pipeline() -> None:
    now_il = datetime.now(tz=_ISRAEL_TZ)
    log.info("\n%s", "=" * 60)
    log.info("Run started: %s", now_il.strftime("%Y-%m-%d %H:%M:%S %Z"))

    async with WinnerScraper(headless=True) as s:
        result = await s.get_all_markets()
    winner_markets = _filter_upcoming_football(result["markets"])

    sport_keys = list({
        WINNER_LEAGUE_MAP[m["league"]]
        for m in winner_markets
        if WINNER_LEAGUE_MAP.get(m["league"])
    })

    pinnacle_markets = _get_pinnacle_odds_cached(sport_keys)
    pairs = match_markets(winner_markets, pinnacle_markets)
    log.info("[Scheduler] %d matched pairs", len(pairs))

    alerts = calculate_ev(pairs)
    log.info("[Scheduler] %d +EV alert(s)", len(alerts))

    event_id_map = {
        f"{p['pinnacle']['home_team']} vs {p['pinnacle']['away_team']}": p["winner"]["event_id"]
        for p in pairs
    }

    new_alerts = []
    for alert in alerts:
        event_id = event_id_map.get(alert["match"])
        if event_id is None:
            new_alerts.append(alert)
            continue
        if not alert_exists(event_id, alert["outcome"], alert["ev_pct"]):
            save_alert(alert, event_id)
            new_alerts.append(alert)
        else:
            log.info("[Scheduler] Duplicate skipped: %s %s EV=+%s%%",
                     alert["match"], alert["outcome"], alert["ev_pct"])

    await send_alerts(new_alerts)

    pending = get_pending_results()
    if pending:
        results = get_results_batch(pending)
        for row in pending:
            result = results.get(row["event_id"])
            if result is not None:
                update_result(row["id"], result)
                log.info("[Scheduler] Result: %s vs %s → %s",
                         row["home"], row["away"], result)


async def main() -> None:
    global agent_ran_today
    log.info("EV Bot scheduler starting.")
    log.info("Active window: 12:00–22:00 IL | Pipeline: every 10 min | Agent: daily at 11:55 IL")
    init_db()
    while True:
        now_il = datetime.now(tz=_ISRAEL_TZ)

        if now_il.hour == 11 and 50 <= now_il.minute < 60 and agent_ran_today != now_il.date():
            try:
                await run_agent()
            except Exception as exc:
                log.error("[Scheduler] Agent run failed: %s", exc, exc_info=True)
            agent_ran_today = now_il.date()

        if _ACTIVE_START <= now_il.hour < _ACTIVE_END:
            try:
                await _run_pipeline()
            except Exception as exc:
                log.error("[Scheduler] Run failed: %s", exc, exc_info=True)
        else:
            log.info("[Scheduler] Outside active window (12:00–22:00 IL). Sleeping.")

        next_run = datetime.now(tz=_ISRAEL_TZ) + _RUN_INTERVAL
        log.info("[Scheduler] Next run: %s", next_run.strftime("%H:%M:%S %Z"))
        log.info("%s", "=" * 60)
        await asyncio.sleep(_RUN_INTERVAL.total_seconds())


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main())

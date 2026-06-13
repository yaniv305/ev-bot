"""
telegram_bot.py — Send +EV alerts to a Telegram chat.
"""
import logging
import os

from dotenv import load_dotenv
from telegram import Bot
from telegram.error import TelegramError

load_dotenv()

log = logging.getLogger(__name__)

_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def _format_alert(alert: dict) -> str:
    return (
        f"🟢 *+EV Alert*\n\n"
        f"⚽ {alert['match']}\n"
        f"🕐 {alert['israel_time']} (Israel)\n"
        f"📊 {alert['market']} — {alert['outcome']}\n"
        f"💰 Winner: {alert['winner_odds']} | Fair: {alert['pinnacle_fair_odds']}\n"
        f"📈 EV: +{alert['ev_pct']}%"
    )


_SPORT_EMOJI = {"soccer": "⚽", "basketball": "🏀", "baseball": "⚾", "tennis": "🎾"}
_SPORT_ORDER = ["soccer", "basketball", "baseball", "tennis"]


def _format_coverage_report(total_matched: int, orphan_report: dict) -> str:
    from datetime import datetime
    date_str = datetime.now().strftime("%Y-%m-%d")

    total_unmatched = sum(
        len(orphan_report.get(s, {}).get("orphan_games", []))
        for s in _SPORT_ORDER
    )

    lines = [f"📊 Coverage Scan — {date_str}", ""]
    lines.append(f"✅ {total_matched} matched | ⚠️ {total_unmatched} unmatched")
    lines.append("")

    for sport in _SPORT_ORDER:
        data = orphan_report.get(sport, {})
        matched = data.get("matched_count", 0)
        unmatched = len(data.get("orphan_games", []))
        emoji = _SPORT_EMOJI.get(sport, "🔵")
        lines.append(f"{emoji} {sport.capitalize()}: {matched}✓  {unmatched}✗")

    matched_pairs_all = [
        (sport, pair)
        for sport in _SPORT_ORDER
        for pair in orphan_report.get(sport, {}).get("pairs", [])
    ]

    if matched_pairs_all:
        lines.append("")
        lines.append("Matched games:")
        for sport, pair in matched_pairs_all:
            emoji = _SPORT_EMOJI.get(sport, "🔵")
            kickoff = pair["kickoff"].split("T")[1][:5]
            lines.append(f"{emoji} {pair['winner_description']} → {pair['pinnacle_name']} | {kickoff}")

    orphan_games_all = [
        (sport, game)
        for sport in _SPORT_ORDER
        for game in orphan_report.get(sport, {}).get("orphan_games", [])
    ]

    if orphan_games_all:
        lines.append("")
        lines.append("Unmatched games:")
        for sport, game in orphan_games_all:
            emoji = _SPORT_EMOJI.get(sport, "🔵")
            kickoff = game["kickoff"].split("T")[1][:5]
            lines.append("")
            pinnacle_league = game.get("pinnacle_league", "")
            league_part = f"{game['league']} → {pinnacle_league}" if pinnacle_league else game["league"]
            lines.append(f"{emoji} {game['description']} | {league_part} | {kickoff}")
            if game.get("reason") == "no_candidates":
                lines.append("  No candidates in ±15 min window")
            else:
                for c in game.get("candidates", []):
                    t = c["commence_time"].split("T")[1][:5]
                    lines.append(f"  • {c['home_team']} vs {c['away_team']} {t}")

    msg = "\n".join(lines)
    if len(msg) > 4000:
        msg = msg[:4000] + "\n... (truncated)"
    return msg


async def send_coverage_report(total_matched: int, orphan_report: dict) -> None:
    """Send the daily coverage scan summary to Telegram."""
    if not _TOKEN or not _CHAT_ID:
        log.warning("[Telegram] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — skipping coverage report")
        return

    text = _format_coverage_report(total_matched, orphan_report)
    bot = Bot(token=_TOKEN)
    try:
        await bot.send_message(chat_id=_CHAT_ID, text=text)
        log.info("[Telegram] Coverage report sent (%d chars)", len(text))
    except TelegramError as exc:
        log.error("[Telegram] Failed to send coverage report: %s", exc)


async def send_alerts(alerts: list[dict]) -> None:
    """
    Send each alert as a separate Telegram message.
    Does nothing if alerts is empty.
    """
    if not alerts:
        return

    if not _TOKEN or not _CHAT_ID:
        log.error("[Telegram] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
        return

    bot = Bot(token=_TOKEN)
    for alert in alerts:
        text = _format_alert(alert)
        try:
            await bot.send_message(
                chat_id=_CHAT_ID,
                text=text,
                parse_mode="Markdown",
            )
            log.info("[Telegram] Sent: %s — %s EV=+%s%%",
                     alert["match"], alert["outcome"], alert["ev_pct"])
        except TelegramError as exc:
            log.error("[Telegram] Failed to send alert for %s: %s",
                      alert["match"], exc)

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

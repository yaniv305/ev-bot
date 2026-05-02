"""
results_fetcher.py — Fetch final scores for finished games from Winner results page.
"""
import logging
from datetime import date, timedelta
from typing import Optional

from playwright_stealth import Stealth

from winner_scraper import SITE_URL, USER_AGENT

log = logging.getLogger(__name__)

_FOOTBALL_SPORT_ID = "240"
_RESULTS_URL_TEMPLATE = (
    "https://www.winner.co.il/%D7%AA%D7%95%D7%A6%D7%90%D7%95%D7%AA/%D7%95%D7%99%D7%A0%D7%A8/"
    "?date={today}&startDate={yesterday}"
)


def _fmt(d: date) -> str:
    return d.strftime("%d-%m-%Y")


def _determine_result(score_a: str, score_b: str) -> Optional[str]:
    """Return '1X2' outcome from final scores, or None if scores are unparseable."""
    try:
        a, b = int(score_a), int(score_b)
    except (ValueError, TypeError):
        return None
    if a > b:
        return "Home"
    if a < b:
        return "Away"
    return "Draw"


async def _fetch_raw_events() -> list[dict]:
    """
    Spin up a stealth browser, warm up at winner.co.il, navigate to the
    results page, and return the raw events list from GetResults.
    """
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout

    today = date.today()
    results_url = _RESULTS_URL_TEMPLATE.format(
        today=_fmt(today), yesterday=_fmt(today - timedelta(days=1))
    )
    captured: dict = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            locale="he-IL",
            viewport={"width": 1280, "height": 800},
            extra_http_headers={"Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7"},
        )
        stealth = Stealth(navigator_languages_override=("he-IL", "he", "en-US", "en"))
        await stealth.apply_stealth_async(context)
        page = await context.new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )

        # Warm up — establish Imperva trust
        try:
            async with page.expect_response(
                lambda r: "GetCMobileLine" in r.url, timeout=45_000
            ):
                await page.goto(SITE_URL, wait_until="networkidle", timeout=45_000)
        except PWTimeout:
            pass  # networkidle can stall; trust is established as long as cookies are set

        # Navigate to results page and intercept GetResults
        async def on_response(response):
            if "GetResults" in response.url and "data" not in captured:
                try:
                    captured["data"] = await response.json()
                except Exception as exc:
                    log.error("[Results] Failed to parse GetResults: %s", exc)

        page.on("response", on_response)
        try:
            await page.goto(results_url, wait_until="networkidle", timeout=30_000)
        except PWTimeout:
            pass
        await browser.close()

    return captured.get("data", {}).get("results", {}).get("events", [])


async def get_result(event_id: int) -> Optional[str]:
    """
    Fetch the final 1X2 result for a single Winner football event.

    Returns "Home", "Draw", "Away", or None if not found / not finished.
    """
    events = await _fetch_raw_events()
    for event in events:
        if str(event.get("eventid")) != str(event_id):
            continue
        if event.get("sportid") != _FOOTBALL_SPORT_ID:
            log.warning("[Results] Event %d sportid=%s — not football",
                        event_id, event.get("sportid"))
            return None
        result = _determine_result(event.get("scoreA"), event.get("scoreB"))
        log.info("[Results] Event %d: %s %s-%s %s → %s",
                 event_id, event["teamA"],
                 event.get("scoreA", "?"), event.get("scoreB", "?"),
                 event["teamB"], result)
        return result

    log.info("[Results] Event %d not found in GetResults", event_id)
    return None


async def get_results_batch(event_ids: list[int]) -> dict[int, Optional[str]]:
    """
    Fetch results for multiple events in a single browser session.

    Returns {event_id: "Home" | "Draw" | "Away" | None}
    """
    results = {eid: None for eid in event_ids}
    if not event_ids:
        return results

    id_set = {str(eid) for eid in event_ids}
    events = await _fetch_raw_events()

    for event in events:
        eid_str = str(event.get("eventid"))
        if eid_str not in id_set:
            continue
        if event.get("sportid") != _FOOTBALL_SPORT_ID:
            continue
        eid = int(eid_str)
        result = _determine_result(event.get("scoreA"), event.get("scoreB"))
        log.info("[Results] Event %d: %s %s-%s %s → %s",
                 eid, event["teamA"],
                 event.get("scoreA", "?"), event.get("scoreB", "?"),
                 event["teamB"], result)
        results[eid] = result

    return results

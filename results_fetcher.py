"""
results_fetcher.py — Fetch finished match results from Winner.co.il.

Intercepts the GetResults API using a warmed stealth browser session,
the same approach as winner_scraper.py. Returns Home/Draw/Away per event_id.
"""
import asyncio
import logging
from typing import Optional

log = logging.getLogger(__name__)

_RESULTS_URL = "https://www.winner.co.il/results"
_FOOTBALL_SPORT_ID = "240"
_INTERCEPT_TIMEOUT = 30_000
_FIRST_LOAD_TIMEOUT = 45_000

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def _determine_result(score_a: str, score_b: str) -> Optional[str]:
    try:
        a, b = int(score_a), int(score_b)
    except (ValueError, TypeError):
        return None
    if a > b:
        return "Home"
    if b > a:
        return "Away"
    return "Draw"


async def get_results_batch(event_ids: list[int]) -> dict[int, Optional[str]]:
    """
    Fetch results for a batch of event IDs in one browser session.
    Returns {event_id: "Home"|"Draw"|"Away"|None}.
    """
    if not event_ids:
        return {}

    target_ids = set(event_ids)
    results: dict[int, Optional[str]] = {eid: None for eid in event_ids}

    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    from playwright_stealth import Stealth

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

        try:
            async with page.expect_response(
                lambda r: "GetResults" in r.url,
                timeout=_INTERCEPT_TIMEOUT,
            ) as resp_info:
                try:
                    await page.goto(
                        _RESULTS_URL,
                        wait_until="networkidle",
                        timeout=_FIRST_LOAD_TIMEOUT,
                    )
                except PWTimeout:
                    pass

            response = await resp_info.value
            data = await response.json()
        except Exception as exc:
            log.error("[ResultsFetcher] Failed to intercept GetResults: %s", exc)
            await browser.close()
            return results

        events = data if isinstance(data, list) else data.get("events") or data.get("data") or []

        for event in events:
            try:
                if str(event.get("sportid", "")) != _FOOTBALL_SPORT_ID:
                    continue
                eid = int(event["eventId"])
                if eid not in target_ids:
                    continue
                result = _determine_result(event.get("scoreA"), event.get("scoreB"))
                if result is not None:
                    results[eid] = result
                    log.info("[ResultsFetcher] event %d → %s (%s–%s)", eid, result,
                             event.get("scoreA"), event.get("scoreB"))
            except (KeyError, ValueError, TypeError):
                continue

        await browser.close()

    return results


async def get_result(event_id: int) -> Optional[str]:
    batch = await get_results_batch([event_id])
    return batch.get(event_id)

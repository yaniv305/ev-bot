"""
winner_scraper.py — Winner.co.il Sports Betting Odds Scraper
------------------------------------------------------------
Uses async Playwright to load winner.co.il in headless Chromium and intercept
the JSON API responses that the site's own JavaScript makes automatically.

Background:
  Imperva Bot Manager blocks all direct HTTP clients (requests, curl-cffi) with
  HTTP 500. The only working strategy is to let the real browser issue the API
  calls under Imperva's trust umbrella and capture the responses in-flight.

API endpoints used:
  Base : https://api.winner.co.il/v2/publicapi
  GetCMobileLine  — full list of available betting markets (intercepted)
  GetDMobileLine  — single market details by per-market checksum (via page.evaluate)

Usage:
    import asyncio
    from winner_scraper import WinnerScraper

    async def main():
        async with WinnerScraper(headless=True) as scraper:
            markets = await scraper.get_all_markets()
            print(f"Got {len(markets)} markets")
            odds = await scraper.get_market_odds(markets[0]["line_checksum"])

    asyncio.run(main())
"""

import asyncio
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Constants ─────────────────────────────────────────────────────────────────

BASE_URL   = "https://api.winner.co.il/v2/publicapi"
SITE_URL   = "https://www.winner.co.il/"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# Timeouts in milliseconds
_FIRST_LOAD_TIMEOUT  = 45_000   # goto networkidle — lets Imperva JS challenge run
_RELOAD_TIMEOUT      = 30_000   # reload domcontentloaded — cookies already trusted
_INTERCEPT_TIMEOUT   = 30_000   # wait for GetCMobileLine response
_EVALUATE_TIMEOUT    = 15_000   # page.evaluate for GetDMobileLine

# Retry policy for get_all_markets()
MAX_RETRIES    = 2    # total extra attempts after first failure
_RETRY_DELAY_S = 3   # seconds to wait between attempts

# Winner.co.il publishes kickoffs in Israeli time — ZoneInfo handles DST automatically
_ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")

# Print one sample kickoff conversion per process run for verification
_kickoff_log_done = False


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_kickoff(e_date, m_hour) -> str:
    """
    Convert raw API date+time fields to a UTC-aware ISO 8601 string.

    e_date : int  — YYMMDD  e.g. 260307  → 2026-03-07
    m_hour : str  — HHMM    e.g. "1544"  → 15:44 Israeli time → 13:44 UTC

    Winner publishes times in Israeli local time (Asia/Jerusalem).
    DST is handled automatically via ZoneInfo.

    Returns empty string on any parse failure so callers always get a str.
    """
    try:
        d = str(e_date).zfill(6)
        h = str(m_hour).zfill(4)
        local_dt = datetime(
            2000 + int(d[0:2]),
            int(d[2:4]),
            int(d[4:6]),
            int(h[0:2]),
            int(h[2:4]),
            tzinfo=_ISRAEL_TZ,
        )
        utc_dt = local_dt.astimezone(timezone.utc)
        # Log once per process run as a sample to confirm UTC conversion is active
        global _kickoff_log_done
        if not _kickoff_log_done:
            print(
                f"  [KICKOFF] Parsed {d[4:6]}/{d[2:4]} {h[0:2]}:{h[2:4]} IST"
                f" → {utc_dt.isoformat(timespec='seconds')} UTC"
            )
            _kickoff_log_done = True
        return utc_dt.isoformat(timespec="seconds")
    except (ValueError, TypeError, IndexError):
        return ""


def _normalise_outcome(raw: dict) -> Optional[dict]:
    """
    Convert a raw outcome object to the clean schema.
    Returns None if the price cannot be parsed as a positive float
    (e.g. "SUSP" for suspended markets).
    """
    try:
        price = float(raw["price"])
        if price <= 0:
            return None
        return {
            "outcome_id": int(raw["outcomeId"]),
            "price":      price,
            "desc":       str(raw.get("desc", "")),
        }
    except (KeyError, ValueError, TypeError):
        return None


def _normalise_market(raw: dict) -> Optional[dict]:
    """
    Normalise a raw GetCMobileLine / GetDMobileLine market object.

    Returns None when mandatory fields (mId, brid) are absent, so callers
    can safely do: [m for m in map(_normalise_market, raw_list) if m].

    The 'brid' field is the per-market reference ID; it maps to the
    lineChecksum parameter for GetDMobileLine calls.
    """
    try:
        outcomes = [
            o for o in (_normalise_outcome(x) for x in raw.get("outcomes") or [])
            if o is not None
        ]
        return {
            "match_id":      int(raw["mId"]),
            "event_id":      int(raw.get("eId", 0)),
            "sport_id":      int(raw.get("sId", 0)),
            "description":   str(raw.get("desc", "")),
            "league":        str(raw.get("league", "")),
            "country":       str(raw.get("country", "")),
            "kickoff":       _parse_kickoff(raw.get("e_date"), raw.get("m_hour", 0)),
            "market_type":   str(raw.get("mp", "")),
            "line_checksum": str(raw["brid"]),
            "outcomes":      outcomes,
        }
    except (KeyError, TypeError):
        return None


def _extract_markets_from_response(data) -> list:
    """
    GetCMobileLine can return:
      - a list of market objects directly
      - a dict { hashes: {...}, markets: [...] }
      - a dict with other wrapper keys (data / items / line / events)

    Returns the raw list (un-normalised) or [] if nothing is found.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("markets", "data", "items", "line", "events"):
            value = data.get(key)
            if isinstance(value, list):
                return value
        if "mId" in data:          # dict is itself a single market
            return [data]
    return []


# ── Scraper class ─────────────────────────────────────────────────────────────

class WinnerScraper:
    """
    Async scraper for Winner.co.il betting markets.

    Keeps a single headless Chromium browser alive across calls to avoid
    re-launching for every market fetch.

    Args:
        headless  : run without a visible browser window (default True).
        sport_ids : optional set of sId values to keep (None = all sports).
                    From observed data: football/soccer uses sId=240.
                    Example: WinnerScraper(sport_ids={240})
    """

    def __init__(
        self,
        headless: bool = True,
        sport_ids: Optional[set] = None,
    ) -> None:
        self._headless    = headless
        self._sport_ids   = set(sport_ids) if sport_ids else None
        self._pw          = None
        self._browser     = None
        self._context     = None
        self._page        = None
        self._warmed_up   = False

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Launch Chromium with anti-detection settings.

        Applies playwright-stealth to the browser context (patches 17 JS
        evasion vectors including navigator.webdriver, plugins, languages,
        WebGL, etc.).  A manual add_init_script for navigator.webdriver is
        also added as belt-and-suspenders.
        """
        if self._pw is not None:
            raise RuntimeError("Scraper already running — call stop() first.")

        from playwright.async_api import async_playwright
        from playwright_stealth import Stealth

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self._headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._context = await self._browser.new_context(
            user_agent=USER_AGENT,
            locale="he-IL",
            viewport={"width": 1280, "height": 800},
            extra_http_headers={
                "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        )

        stealth = Stealth(
            navigator_languages_override=("he-IL", "he", "en-US", "en"),
        )
        await stealth.apply_stealth_async(self._context)

        self._page = await self._context.new_page()
        await self._page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )

    async def stop(self) -> None:
        """Close the browser and release all Playwright resources."""
        self._warmed_up = False
        for attr in ("_browser", "_pw"):
            obj = getattr(self, attr, None)
            if obj:
                try:
                    await obj.close() if attr == "_browser" else await obj.stop()
                except Exception:
                    pass
        self._pw = self._browser = self._context = self._page = None

    async def __aenter__(self) -> "WinnerScraper":
        await self.start()
        return self

    async def __aexit__(self, *_) -> None:
        await self.stop()

    # ── Public API ─────────────────────────────────────────────────────────────

    async def _navigate_and_intercept(self):
        """
        Perform one navigation (goto or reload) and capture the GetCMobileLine
        response.  Records `fetched_at` immediately after the response arrives.

        Returns:
            (response, fetched_at_utc)

        Raises:
            playwright TimeoutError  — if GetCMobileLine is not intercepted in time
            Any other exception      — propagated to caller for retry handling
        """
        from playwright.async_api import TimeoutError as PWTimeout

        async with self._page.expect_response(
            lambda r: "GetCMobileLine" in r.url,
            timeout=_INTERCEPT_TIMEOUT,
        ) as resp_info:
            if not self._warmed_up:
                print("  [first load] Navigating to winner.co.il ...", flush=True)
                try:
                    await self._page.goto(
                        SITE_URL,
                        wait_until="networkidle",
                        timeout=_FIRST_LOAD_TIMEOUT,
                    )
                except PWTimeout:
                    # networkidle can stall on heavy SPAs; acceptable as long as
                    # the API call was still issued and expect_response catches it.
                    print("  [warn] networkidle timed out — still waiting for API response ...",
                          file=sys.stderr)
                self._warmed_up = True
            else:
                print("  [reload] Refreshing page ...", flush=True)
                try:
                    await self._page.reload(
                        wait_until="domcontentloaded",
                        timeout=_RELOAD_TIMEOUT,
                    )
                except PWTimeout:
                    print("  [warn] reload timed out — still waiting for API response ...",
                          file=sys.stderr)

        response = await resp_info.value
        fetched_at = datetime.now(tz=timezone.utc)   # Fix 2: record immediately after intercept
        return response, fetched_at

    async def get_all_markets(self) -> dict:
        """
        Fetch all available betting markets from Winner.co.il.

        Navigates to winner.co.il (or reloads on subsequent calls) and intercepts
        the GetCMobileLine JSON response that the site's own JavaScript makes.

        Retries up to MAX_RETRIES times on failure, waiting _RETRY_DELAY_S seconds
        between attempts.

        Returns:
            dict with keys:
              "markets"        : list of normalised market dicts (may be [])
              "fetched_at"     : UTC datetime when the response was intercepted
              "source"         : "winner"
              "session_warmed" : True if session was already established (reload),
                                 False on cold first load
              "error"          : str — only present when all attempts failed
        """
        if self._page is None:
            print("[WinnerScraper] ERROR: not started. Use start() or async with.",
                  file=sys.stderr)
            return {
                "markets": [], "fetched_at": datetime.now(tz=timezone.utc),
                "source": "winner", "session_warmed": False,
                "error": "Scraper not started",
            }

        session_was_warm = self._warmed_up
        last_error = "unknown error"

        for attempt in range(1, MAX_RETRIES + 2):   # e.g. 1, 2, 3 for MAX_RETRIES=2
            try:
                response, fetched_at = await self._navigate_and_intercept()
            except Exception as exc:
                last_error = (
                    f"GetCMobileLine not intercepted within {_INTERCEPT_TIMEOUT // 1000}s"
                    if "TimeoutError" in type(exc).__name__
                    else str(exc)
                )
                if attempt <= MAX_RETRIES:
                    print(f"  [Winner] Attempt {attempt} failed — retrying in {_RETRY_DELAY_S}s ...")
                    await asyncio.sleep(_RETRY_DELAY_S)
                else:
                    print(f"  [Winner] Attempt {attempt} failed — giving up")
                continue

            try:
                data = await response.json()
            except Exception as exc:
                last_error = f"JSON parse error: {exc}"
                print(f"[WinnerScraper] ERROR parsing GetCMobileLine JSON: {exc}", file=sys.stderr)
                if attempt <= MAX_RETRIES:
                    print(f"  [Winner] Attempt {attempt} failed — retrying in {_RETRY_DELAY_S}s ...")
                    await asyncio.sleep(_RETRY_DELAY_S)
                else:
                    print(f"  [Winner] Attempt {attempt} failed — giving up")
                continue

            # Success — normalise and return
            raw_markets = _extract_markets_from_response(data)
            markets = [m for m in map(_normalise_market, raw_markets) if m is not None]
            if self._sport_ids is not None:
                markets = [m for m in markets if m["sport_id"] in self._sport_ids]

            warmth  = "warm" if session_was_warm else "cold"
            ts_str  = fetched_at.strftime("%Y-%m-%dT%H:%M:%SZ")
            print(f"  [Winner] Fetched {len(markets)} markets at {ts_str} (session: {warmth})")

            return {
                "markets":        markets,
                "fetched_at":     fetched_at,
                "source":         "winner",
                "session_warmed": session_was_warm,
            }

        # All attempts exhausted
        retries_label = f"{MAX_RETRIES} {'retry' if MAX_RETRIES == 1 else 'retries'}"
        error_msg = f"{last_error} after {retries_label}"
        return {
            "markets":        [],
            "fetched_at":     datetime.now(tz=timezone.utc),
            "source":         "winner",
            "session_warmed": session_was_warm,
            "error":          error_msg,
        }

    async def get_market_odds(self, line_checksum: str) -> Optional[dict]:
        """
        Fetch detailed odds for a specific market via GetDMobileLine.

        Executes the HTTP request from within the live browser page using
        page.evaluate(fetch(...)).  This is critical: because the page already
        holds valid Imperva session cookies (established during get_all_markets),
        the fetch inherits them via credentials="include" and is treated as a
        legitimate in-page XHR call from https://www.winner.co.il.

        Note: get_all_markets() already includes outcomes/prices for each market.
        Use this method when you need the raw GetDMobileLine payload (e.g. for
        markets with additional sub-markets not fully listed in GetCMobileLine).

        Args:
            line_checksum: the `line_checksum` value from a market dict returned
                           by get_all_markets() — maps to the `brid` field in the
                           raw API (e.g. "61376611").

        Returns:
            Normalised market dict, or None on failure.
        """
        if self._page is None or not self._warmed_up:
            print(
                "[WinnerScraper] ERROR: call get_all_markets() first to establish "
                "a valid browser session.",
                file=sys.stderr,
            )
            return None

        url = f"{BASE_URL}/GetDMobileLine?lineChecksum={line_checksum}"

        # The URL is passed as an argument to avoid f-string collisions with
        # the JS object literal braces and to prevent any injection issues.
        js = """async (url) => {
            try {
                const resp = await fetch(url, {
                    method: "GET",
                    credentials: "include",
                    headers: {
                        "Accept": "application/json, text/plain, */*",
                        "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8"
                    }
                });
                const body = await resp.text();
                return { ok: resp.ok, status: resp.status, body: body };
            } catch (err) {
                return { ok: false, status: 0, body: String(err) };
            }
        }"""

        try:
            result = await self._page.evaluate(js, url)
        except Exception as exc:
            print(f"[WinnerScraper] ERROR in page.evaluate: {exc}", file=sys.stderr)
            return None

        if not result.get("ok"):
            status = result.get("status", "?")
            preview = (result.get("body") or "")[:200]
            print(
                f"[WinnerScraper] GetDMobileLine returned HTTP {status} "
                f"for checksum={line_checksum}.\n"
                f"  Body preview: {preview}\n"
                "  Note: if status=500, the Imperva session may have expired — "
                "call get_all_markets() to refresh. The outcomes already present "
                "in get_all_markets() results may be sufficient.",
                file=sys.stderr,
            )
            return None

        try:
            raw = json.loads(result["body"])
        except (json.JSONDecodeError, KeyError) as exc:
            print(f"[WinnerScraper] ERROR parsing GetDMobileLine JSON: {exc}",
                  file=sys.stderr)
            return None

        # GetDMobileLine may return a single market object or a list
        if isinstance(raw, list):
            return _normalise_market(raw[0]) if raw else None
        return _normalise_market(raw)


# ── Filter ────────────────────────────────────────────────────────────────────

def _filter_upcoming_football(markets: list) -> list:
    """Return football markets (sport_id=240) kicking off within the next 30 minutes (UTC)."""
    now = datetime.now(tz=timezone.utc)
    cutoff = now + timedelta(minutes=30)
    result = []
    for m in markets:
        if m["sport_id"] != 240:
            continue
        try:
            kickoff_dt = datetime.fromisoformat(m["kickoff"])
        except (ValueError, KeyError):
            continue
        if now <= kickoff_dt <= cutoff:
            result.append(m)
    return result


# ── Main (test run) ───────────────────────────────────────────────────────────

async def _main() -> None:
    print("=" * 60)
    print("WinnerScraper — Market Discovery")
    print("=" * 60)
    print()
    print("Starting headless browser ...")
    print("(First load takes 15-30 s — Imperva JS challenge)")
    print()

    async with WinnerScraper(headless=True) as scraper:

        # ── Step 1: fetch all markets ─────────────────────────────────────────
        result = await scraper.get_all_markets()

        if "error" in result:
            print(f"\n[FAIL] Winner fetch failed: {result['error']}")
            return

        markets = _filter_upcoming_football(result["markets"])
        if not markets:
            print("\n[FAIL] No markets returned. Check stderr for details.")
            return

        print(f"\n[OK] Total markets fetched: {len(markets)}")

        # Sport ID distribution — helps identify which sId is football
        sport_counts = Counter(m["sport_id"] for m in markets)
        print(f"\nSport ID distribution (top 10 by count):")
        for sid, count in sport_counts.most_common(10):
            print(f"  sId={sid:>6}  —  {count:>4} markets")

        # ── Step 2: print first 3 markets ─────────────────────────────────────
        print(f"\n{'─' * 60}")
        print("First 3 markets:")
        print(f"{'─' * 60}")
        for i, m in enumerate(markets[:3]):
            print(f"\n[Market {i + 1}]")
            print(json.dumps(m, indent=2, ensure_ascii=False))

        # ── Step 3: test get_market_odds on first market ───────────────────────
        first = markets[0]
        checksum = first["line_checksum"]
        print(f"\n{'─' * 60}")
        print(f"Testing get_market_odds(line_checksum={checksum!r})")
        print(f"{'─' * 60}")

        odds = await scraper.get_market_odds(checksum)
        if odds:
            print("[OK] GetDMobileLine result:")
            print(json.dumps(odds, indent=2, ensure_ascii=False))
        else:
            print("[INFO] get_market_odds returned None (see stderr).")
            print("       The outcomes from get_all_markets() are sufficient for EV calculation.")

    print("\n" + "=" * 60)
    print("Done.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(_main())

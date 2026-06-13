# EV Bot — Project Case Study

*A comprehensive technical reference for resume representation and interview preparation.*

---

## 1. Project Overview

### Problem Being Solved

Sports betting markets are not equally efficient. Pinnacle Sports is widely regarded as the sharpest bookmaker in the world — they accept large bets from professional players and set odds that closely reflect true event probabilities. Winner.co.il is a large Israeli retail bookmaker whose odds frequently lag behind Pinnacle's, especially on in-play and near-kickoff markets.

When Winner offers higher odds than the probability implied by Pinnacle's lines, the expected value of the bet is positive. This discrepancy is tradable: a bettor placing consistently +EV bets will profit in the long run regardless of individual outcomes. The problem is that these windows are narrow — they appear and disappear within minutes — and scanning hundreds of live markets manually is not feasible.

### Why the Project Was Built

To automate the identification of +EV betting opportunities in real time, without requiring manual monitoring. The system runs continuously during the active betting window (12:00–22:00 Israel time) and sends an alert to Telegram the moment a qualified opportunity appears.

A secondary problem emerged during development: it was not known in advance which Winner leagues had Pinnacle coverage at all, or how Winner's Hebrew league names mapped to Pinnacle's English sport keys. This motivated a separate daily coverage scan that discovers all possible Winner↔Pinnacle matches automatically without a pre-built map.

### End Users and Expected Value

The primary user is the developer themselves. The system eliminates the need to manually monitor odds across two platforms, ensures no opportunity is missed due to human inattention, and provides a persistent log of all alerts including final match outcomes for retrospective EV validation.

---

## 2. System Architecture

### High-Level Architecture

The system has two parallel tracks:

**Live EV pipeline** (runs every 20 minutes, 12:00–22:00 IL):
```
WinnerScraper → matcher.py → ev_calculator.py → telegram_bot.py
                     ↕                                   ↕
               pinnacle_client.py                   database.py
```

**Daily coverage scan** (runs once at 08:00 IL):
```
WinnerScraper → coverage_scan_agent.py (4 parallel sport agents)
                        ↕                        ↕
               The Odds API /events      Claude Haiku (league + game matching)
                        ↓
           data/coverage_{date}.json + data/orphans_{date}.json
                        ↓
                telegram_bot.py (coverage report)
```

**Daily prep agent** (runs at 11:50 IL):
```
agents/daily_prep_agent.py → verifies leagues, translations, coverage before active window
```

### Main Components

| Component | File | Responsibility |
|---|---|---|
| Winner Scraper | `winner_scraper.py` | Playwright browser — intercepts Winner's internal `GetCMobileLine` API |
| Matcher | `matcher.py` | Matches Winner games to Pinnacle games (soccer-only, uses pre-built league map) |
| Pinnacle Client | `pinnacle_client.py` | Fetches live odds from The Odds API |
| EV Calculator | `ev_calculator.py` | Computes EV for each 1X2 outcome; filters to ≥ 4% threshold |
| Telegram Bot | `telegram_bot.py` | Sends EV alerts and coverage reports to a Telegram channel |
| Database | `database.py` | SQLite — deduplicates alerts, stores match outcomes |
| Results Fetcher | `results_fetcher.py` | Scrapes Winner results pages to record final scores |
| Scheduler | `scheduler.py` | Async main loop — orchestrates the pipeline and agents |
| Coverage Scan Agent | `agents/coverage_scan_agent.py` | Multi-sport Claude Haiku agent loop — discovers all Winner↔Pinnacle pairs without a pre-built map |
| Daily Prep Agent | `agents/daily_prep_agent.py` | Pre-flight checks before the active betting window |

### External Services and APIs

**Winner.co.il (unofficial)**: No public API exists. The scraper uses Playwright to load the Winner website, intercepts the internal `GetCMobileLine` XHR response (JSON), and parses the markets payload. The site is protected by Imperva bot detection, bypassed via `playwright-stealth`.

**The Odds API** (`api.the-odds-api.com/v4`):
- `GET /v4/sports` — list all active sport keys (free)
- `GET /v4/sports/{key}/events` — get events with IDs and kickoffs (free, zero quota cost)
- `GET /v4/sports/{key}/odds` — live 1X2 odds (costs 1 quota unit per sport key per call)

The coverage scan uses only the free endpoints to avoid quota consumption. The live pipeline uses the paid `/odds` endpoint but applies a 20-minute in-memory cache to minimize quota usage.

**Anthropic API (Claude Haiku)**: Used for all Hebrew-to-English matching — both team-level translation in the live pipeline and full game/league matching in the coverage scan.

**Telegram Bot API**: Async message delivery via `python-telegram-bot`. Two message types: structured +EV alerts (Markdown) and the daily coverage scan report (plain text).

**SQLite** (`alerts.db`): Local database. Stores every alert with its event_id, outcome, EV percentage, and eventual match result. Used for deduplication — the same alert is not re-sent within a session.

### Shared State

`translations.json` is the central shared-state file:
- `team_name_cache`: Hebrew → English team name translations (persistent across runs)
- `league_map` / `WINNER_LEAGUE_MAP`: Hebrew league → Pinnacle sport key (soccer-only, hand-curated + verified by daily prep agent)
- `winner_sport_ids`: Winner internal numeric IDs for Basketball, Tennis, Baseball (bootstrapped once via Haiku classification)

All writes to `translations.json` use an atomic rename pattern (write to `.tmp`, then `os.rename()`) to prevent corruption on crash.

---

## 3. Development Process

### Evolution from Initial Idea

The project started as a simple question: can Pinnacle's odds be used as a no-vig probability estimate to find value on Winner? The first implementation was a manual process — fetch odds from both sides, compare, note discrepancies. This immediately validated the concept but showed that the interesting opportunities appear close to kickoff and disappear within minutes.

The first automated version scraped Winner via `requests` with static headers. This worked briefly before Imperva's bot detection began blocking it. Switching to Playwright (headless Chrome) with `playwright-stealth` resolved this, but introduced session management complexity.

The second major phase was matching: Winner uses Hebrew team names (e.g., "מנצ'סטר סיטי") and Pinnacle uses English (e.g., "Manchester City"). The first approach was a static dictionary of ~200 team names. This was immediately brittle — any team not in the dictionary caused a miss. Replacing this with Claude Haiku made the translation dynamic and self-correcting.

The third phase recognized that the static league map (`WINNER_LEAGUE_MAP`) was a bottleneck for multi-sport expansion. A new daily coverage scan agent was built that discovers all possible Winner↔Pinnacle matches from scratch using a Claude Haiku agent loop, eliminating the need for a pre-built map for the scan's purposes.

### Major Design Decisions

**Playwright over requests**: Imperva's bot protection checks browser fingerprints, JavaScript execution, and TLS fingerprints. A bare `requests` client fails these. Playwright with stealth provides a real browser fingerprint. The scraper is kept alive as a persistent session across pipeline runs to avoid the 3–5 second cold-start overhead on every poll.

**Claude Haiku for team matching instead of a static dictionary or embeddings**: Three approaches were evaluated:
1. Static dictionary — fast but brittle; requires manual maintenance and misses new teams.
2. Sentence embeddings (multilingual model) — added complexity, 470MB model download, and no clear accuracy improvement over LLM. Tested, then discarded.
3. Claude Haiku — handles phonetic Hebrew transliterations natively ("מנצ'סטר סיטי" → "Manchester City"), reasons about abbreviations (ה.י.ק. = HJK), and can distinguish similar names. Results are cached permanently in `translations.json`.

**No-vig probability from Pinnacle**: Pinnacle builds in a small margin (overround). To get true probabilities, the overround is removed: `overround = 1/h + 1/d + 1/a`, then `true_prob = (1/odds) / overround`. This is the standard sharp-money conversion.

**Coverage scan uses only free API endpoints**: The paid `/odds` endpoint costs quota. The coverage scan's goal is to discover *which games* are on both platforms, not to fetch live odds. The free `/events` endpoint returns event IDs and kickoffs — sufficient for matching. This keeps the daily scan at zero quota cost.

**Async architecture for parallel sport agents**: The coverage scan runs four sport agents simultaneously (Soccer, Basketball, Tennis, Baseball). Each agent is CPU-light but I/O-bound (Haiku API calls). Wrapping each in `asyncio.to_thread()` allows true parallelism without blocking the event loop. The orchestrator uses `asyncio.gather()` to await all four.

**SQLite over a cloud database**: The bot is self-hosted. SQLite is sufficient for the volume (dozens of alerts per day), requires zero infrastructure, and can be inspected directly with standard tools. A cloud DB would add latency and operational complexity for no benefit.

### Challenges Encountered

**Imperva bot detection**: The Imperva WAF challenges JavaScript execution, checks browser API consistency, and monitors behavioral patterns. `playwright-stealth` patches ~15 browser API inconsistencies (e.g., `navigator.webdriver`, Canvas fingerprinting, language headers) that automated browsers expose by default. Getting a clean pass required both the stealth library and careful session management.

**Hebrew phonetic transliteration is non-trivial**: Hebrew renders English loan-words phonetically. "טורנמנט" = "tournament", "אילבס" = "Ilves" (Finnish team), "ה.י.ק." = "HJK" (initials of Finnish acronym). No rule-based system handles all cases. This is exactly the kind of task LLMs are well-suited for — they internalize these phonetic mappings from training data.

**Haiku reasoning before answering (token truncation bug)**: When prompted to return a 32-character hex ID, Haiku would sometimes produce reasoning chains first ("The Hebrew name translates to...") consuming 100–120 tokens, then output the hex ID, then get truncated at `max_tokens=128` before completing. The fix was twofold: move the format instruction to the top of the prompt ("Return ONLY the hex id. No explanation.") and raise `max_tokens` to 256. Additionally, a `re.search(r'\b([0-9a-f]{32})\b', raw)` extraction was added as a safety net instead of using the raw response text directly.

**Winner returns multiple markets per event**: A single soccer game on Winner has 1X2, Asian handicap, totals, and dozens of other markets — all sharing the same `event_id`. Passing all of them to the matcher caused ~40x duplicate Haiku calls. Fixed by deduplicating by `event_id` before the matching loop.

**Wrong Pinnacle key detection**: The coverage scan agent, running autonomously, sometimes mapped a Winner league to a plausible but incorrect Pinnacle sport key (e.g., "שבדית שלישית" → Swedish Superettan instead of a lower division). When all games in a league have no Pinnacle candidates in the ±15min window, the key is almost certainly wrong. A majority-based detection was implemented: if `no_cand_count > (total_unmatched - no_cand_count)`, signal the agent to retry with a different key. The condition is majority-based rather than absolute because some Superettan games happened to fall inside the time window by chance, preventing a clean "all-or-nothing" detection.

**Ambiguous single-name matching**: "אילבס - טורון" (Ilves vs TPS Turku) failed repeatedly. Both candidates in the Veikkausliiga had "Turku" in their names ("FC Inter Turku" and "TPS Turku"), causing Haiku to return NO_MATCH rather than guess. The solution was a single-team fallback: when the full description fails, split on " - " and retry with only the home team name ("אילבס" alone → unambiguous match to Ilves Tampere), then if necessary with only the away team. This resolved the case completely.

**Outright markets contaminating the game list**: Winner's data includes specials like "הזוכה במונדיאל 2026" (World Cup 2026 winner — a futures market) inside the same soccer data bucket as match markets. These were being sent to Haiku for game matching and wasting API calls. Fixed with a filter (`_is_game()`) that rejects descriptions that lack the "Home - Away" separator or contain a 4-digit year on either side.

### Trade-offs Considered

**Polling interval**: Originally 10 minutes, changed to 20 minutes to reduce Odds API quota consumption. The lost granularity is acceptable because the betting opportunities this system targets are typically available for at least 15–30 minutes before kickoff.

**Stale NO_MATCH caching**: When Claude returns NO_MATCH for a team translation, the result is cached for the current day only (not permanently). This avoids re-querying Claude for genuinely absent teams while ensuring that temporarily-missing teams (e.g., promoted clubs newly added to Pinnacle) are retried the next day.

**Cache invalidation on miss**: In the live pipeline, if a previously-cached translation fails to produce a game match, both team translations are deleted from cache and re-fetched from Claude (logged as `[RETRANSLATED]`). This handles the case where a cached translation was correct at one time but Pinnacle subsequently renamed the team.

### Features Rejected

**Embedding-based pre-filtering**: Tested `sentence-transformers` with both a multilingual model (470MB) and an English-only model after LLM translation. Neither improved match rates — the LLM itself was already the accurate component, and embeddings added latency and complexity. Discarded.

**Extending `matcher.py` to multi-sport**: The existing `matcher.py` uses a hand-curated `WINNER_LEAGUE_MAP`. Rather than expanding this map to Basketball/Tennis/Baseball (which would require ongoing manual maintenance), the coverage scan agent was built as a separate system that discovers mappings dynamically. The two systems co-exist: `matcher.py` handles the time-critical live pipeline (soccer only), the coverage scan agent handles discovery across all sports.

**Cloud hosting**: Running the bot on a VPS or cloud function was considered. Rejected in favor of local execution because the Playwright browser session has significant startup cost, the polling interval is 20 minutes (not latency-sensitive enough to require cloud), and local execution keeps all credentials off external infrastructure.

---

## 4. Technical Implementation

### Languages and Libraries

- **Python 3.12** — asyncio for the scheduler and parallel sport agents; `zoneinfo` for Israel timezone handling; `pathlib` for file management; `re` for hex ID extraction.
- **Playwright** (`playwright`, `playwright-stealth`) — headless Chromium automation with Imperva bypass.
- **Anthropic SDK** (`anthropic`) — Claude Haiku API calls for both translation and agent loops.
- **`requests`** — synchronous HTTP for The Odds API (used inside threads, not the async loop).
- **`python-telegram-bot`** — async Telegram Bot API client.
- **`sqlite3`** (stdlib) — alert deduplication and result storage.
- **`python-dotenv`** — environment variable management.

### Key Algorithms and Logic

**EV calculation**:
```python
overround = 1/h + 1/d + 1/a
true_prob  = (1/odds) / overround
ev         = (true_prob * winner_odds) - 1
# Alert if ev >= 0.04 (4%)
```

**Haiku hex ID extraction** (covers reasoning-before-answer cases):
```python
hex_match = re.search(r'\b([0-9a-f]{32})\b', raw)
returned_id = hex_match.group(1) if hex_match else "NO_MATCH"
```

**Wrong-key majority detection**:
```python
no_cand_count = sum(1 for g in unmatched if g["reason"] == "no_candidates")
wrong_key = (not pairs and bool(unmatched) and
             no_cand_count > (len(unmatched) - no_cand_count))
```

**Single-team fallback matching**:
```python
parts = description.split(" - ")
if len(parts) == 2:
    for side, heb in [("home", parts[0]), ("away", parts[1])]:
        fallback_id = _match_single_team(client, heb, candidates, side)
        if next((c for c in candidates if c["id"] == fallback_id), None):
            returned_id = fallback_id
            break
```

**Outright market filter** (soccer only):
```python
def _is_game(description: str) -> bool:
    if " - " not in description:
        return False
    parts = description.split(" - ", 1)
    return not any(re.search(r'\b\d{4}\b', p) for p in parts)
```

**Kickoff window filter** (±15 minutes):
```python
candidates = [
    pe for pe in pinnacle_events
    if abs(winner_ko - datetime.fromisoformat(pe["commence_time"].replace("Z", "+00:00")))
       <= timedelta(minutes=15)
]
```

### Data Flow

**Live pipeline (every 20 min)**:
1. `WinnerScraper.get_all_markets()` — Playwright page reload, XHR intercept → raw market list.
2. `_filter_upcoming_football()` — restrict to football markets kicking off within 30 minutes.
3. `get_pinnacle_odds_cached()` — fetch or serve from 20-min in-memory cache.
4. `match_markets()` — filter to 1X2 markets, deduplicate by `event_id`, translate team names via Haiku (with translation cache), apply kickoff proximity check.
5. `calculate_ev()` — remove Pinnacle overround, compute EV per outcome.
6. `alert_exists()` / `save_alert()` — SQLite deduplication gate.
7. `send_alerts()` — Telegram delivery.
8. `get_results_batch()` — scrape results for pending alerts.

**Coverage scan (08:00 IL, once daily)**:
1. `_fetch_winner_games()` — single Winner scraper run across all sports.
2. Four `asyncio.to_thread()` calls launch sport agents in parallel.
3. Each agent: Haiku agent loop → `fetch_pinnacle_events` tool → `match_games_in_league` tool → `return_results` tool.
4. `match_games_in_league`: per-game Haiku call → hex ID extraction → single-team fallback if NO_MATCH → wrong-key majority check → retry signal if needed.
5. `_write_output()` → `data/coverage_{date}.json` (atomic).
6. `_write_orphans()` → `data/orphans_{date}.json` (atomic).
7. `send_coverage_report()` → Telegram summary.

### Error Handling and Reliability

- **Browser failures**: If the Playwright scraper throws or returns an error payload, the scheduler calls `_reset_scraper()` which tears down and nullifies the session. The next run starts a fresh browser.
- **Scraper stealth session**: The browser is never relaunched between runs — only reloaded. This preserves cookies and avoids repeated cold-start bot challenges.
- **Haiku API failures**: All Claude API calls are wrapped in `try/except`. A failed call is logged and the game is skipped (not retried), preventing pipeline stalls.
- **Atomic file writes**: `translations.json` and all `data/*.json` files are written via `.tmp` + `os.rename()`. A crash during write leaves the previous valid file intact.
- **SQLite deduplication**: The same alert (same `event_id`, same outcome, same EV) is not re-sent. This prevents spam when the same opportunity persists across multiple pipeline cycles.
- **NO_MATCH daily caching**: Claude's NO_MATCH verdict for a team translation is cached for the current calendar day only, preventing repeated API calls while allowing retry the following day.
- **Wrong-key retry cap**: `already_retried: set[str]` ensures a winner league is retried with a different Pinnacle key at most once. On second failure it becomes an orphan league, preventing infinite retry loops.

### Caching Strategies

| Cache | Scope | TTL | Storage |
|---|---|---|---|
| Team name translations | Per Hebrew team name | Permanent (NO_MATCH: 1 day) | `translations.json` |
| Pinnacle live odds | Per set of sport keys | 20 minutes | In-memory (`_pinnacle_cache`) |
| Winner sport IDs | Per sport name | Permanent | `translations.json["winner_sport_ids"]` |
| Playwright browser session | Single instance | Lifetime of scheduler process | Module-level `_scraper` |

---

## 5. AI Usage

### How AI Was Used During Development

Claude (Claude Sonnet 4.6 via Claude Code) served as the primary development environment for this project — writing the initial implementation, debugging issues, iterating on prompts, and designing the agent architecture. The development methodology was conversational: describe a problem or desired behavior, implement, test, debug, and refine.

### LLM Components in the Runtime System

**Team name translation** (`matcher.py` — live pipeline): Claude Haiku receives a Hebrew team name and a list of English Pinnacle team names in the same league. It returns the single matching name or `NO_MATCH`. Result is cached in `translations.json`.

**Game matching** (`coverage_scan_agent.py` — coverage scan): For each Winner game event, Haiku receives the Hebrew description ("Home - Away" format) and a list of Pinnacle candidate events (id, teams, kickoff). It returns the 32-character Pinnacle event hex ID or `NO_MATCH`.

**Single-team fallback** (`_match_single_team`): When full-description matching fails, Haiku receives only one Hebrew team name (home or away) and only the corresponding column from the candidate list. This reduces the ambiguity surface area.

**League and sport classification** (coverage scan agent loop): The Haiku agent reasons about which Winner Hebrew leagues correspond to which Pinnacle sport keys, using the Pinnacle key's title and sample team names as evidence. This is fully autonomous — the agent decides which tool calls to make and in what order.

**Winner sport ID bootstrap**: On first run, a single Haiku call classifies Winner's internal numeric sport IDs (e.g., 227 → Basketball) by seeing a sample of Hebrew game descriptions from each ID. Result is cached permanently.

### Prompt Engineering Decisions

**Format instruction at top**: Haiku reasons before answering when given complex prompts. Placing the format instruction first ("Return ONLY the 32-character hex id. No explanation.") causes it to constrain its own output before reasoning begins, reducing token waste and truncation risk.

**Explicit transliteration guidance**: The matching prompts include: "Hebrew names are phonetic transliterations of English. Dotted abbreviations like ה.י.ק. = HJK (initials). The Hebrew format is always 'home team - away team'." This primes Haiku with the exact knowledge needed for this domain.

**Strong-candidate preference**: "Single strong candidate: prefer returning the id over NO_MATCH." This tilts Haiku toward action rather than abstention when confidence is reasonable, improving match rates without materially increasing false positives.

**Single-team prompt shows only the relevant column**: When doing the home-team fallback, only the home team column of candidates is shown, not "home vs away". This eliminates the confounding factor that caused full-description matching to fail.

**Agent system prompt rules**: The coverage scan agent's system prompt includes: "If `match_games_in_league` returns `warning=all_no_candidates`, the Pinnacle key was wrong: try a different key for that winner league." This makes the retry behavior reliable without needing to hard-code it in tool logic.

**Low `max_tokens` for simple lookups**: The single-team fallback uses `max_tokens=64` (sufficient for a 32-char hex ID). The main game matching uses `max_tokens=256` (room for brief reasoning then the ID). The agent loop uses the default (reasoning-intensive). Right-sizing `max_tokens` prevents both truncation and unnecessary cost.

### Where Deterministic Logic Was Preferred Over AI

- **Kickoff proximity filter**: A strict ±15-minute window is applied before Haiku sees any candidates. This eliminates the risk of Haiku matching games from different time slots that happen to have similar team names.
- **Overround removal**: Mathematical formula — no ambiguity.
- **Event deduplication**: Set-based `event_id` deduplication before the matching loop — not an LLM task.
- **Outright market detection** (`_is_game`): A regular expression checking for ` - ` and 4-digit years — deterministic and fast, avoids wasting a Haiku call on a futures market.
- **Hex ID extraction**: `re.search(r'\b([0-9a-f]{32})\b', raw)` — deterministic extraction from potentially verbose Haiku output. LLMs are trusted for reasoning but not for output formatting.
- **Wrong-key majority threshold**: The `no_cand_count > len(unmatched) - no_cand_count` calculation is deterministic. The *decision* to retry is programmatic; only the *retry action* (choosing a different key) is delegated to the agent.

---

## 6. Personal Contribution

This project was designed, built, and maintained by a single developer end-to-end.

**Architecture**: Defined the two-track architecture (live EV pipeline + daily coverage scan), the agent-based approach for coverage discovery, and the clean separation between soccer-specific matching (`matcher.py`) and the multi-sport discovery system (`coverage_scan_agent.py`).

**Implementation**: Wrote all components from scratch: the Playwright scraper with Imperva bypass, the LLM-based team matching with translation caching, the EV calculator with overround removal, the Telegram alert system, the SQLite persistence layer, the results fetcher, the async scheduler, and both agents.

**Prompt engineering**: Designed and iteratively refined all Haiku prompts — format-first instruction placement, transliteration guidance, single-team fallback prompt structure, agent system prompt rules for wrong-key retry.

**Debugging**: Diagnosed and fixed multiple subtle bugs: the `max_tokens=128` truncation causing silent Haiku ID failures across five Finnish games; the duplicate market submission causing 303 records for 36 actual games; the wrong-key detection missing mixed-result cases requiring a majority-based condition; the outright market contamination; the ambiguous two-Turku game requiring a single-team fallback architecture.

**Testing**: All testing was done through live system runs against real Winner and Pinnacle data, comparing output across iterations. Each bug was identified by observing discrepancies between expected and actual match counts, then traced to root cause through log analysis.

**Deployment decisions**: Chose local execution over cloud hosting (persistent browser session, no external credential storage); chose SQLite over a server database; chose polling with caching over webhooks (Pinnacle has no webhook API).

---

## 7. Demonstrated Skills

### Python Development
Built a production-quality async Python application using `asyncio` for concurrent execution, `zoneinfo` for timezone-aware scheduling, `pathlib` for cross-platform file management, `re` for reliable LLM output parsing, and `sqlite3` for persistent storage — all within a single coherent codebase without a web framework.

### API Integration
Integrated four distinct external APIs simultaneously: Winner's undocumented internal API (reverse-engineered and accessed via Playwright interception), The Odds API (two endpoint types with different quota models), Anthropic's API (synchronous Haiku calls inside async threads), and Telegram Bot API (async message delivery). Designed a caching layer for the quota-limited API.

### Automation
Automated a complete end-to-end workflow: browser-based data extraction → multi-step cross-platform matching → mathematical EV calculation → conditional Telegram alerting → outcome result recording. The system runs continuously and requires no human intervention during operation.

### System Design
Designed a two-track architecture that cleanly separates real-time concerns (live EV pipeline) from daily discovery (coverage scan). Made deliberate decisions about what to cache, what to delegate to LLMs, what to compute deterministically, and how to handle failure at each component boundary. Avoided premature abstraction: the soccer-specific matcher and the multi-sport agent are separate components rather than a single over-generalized system.

### Problem Solving
Identified and resolved six distinct non-obvious bugs during development: token truncation in Haiku responses, market deduplication, Imperva bot detection, wrong-key majority detection in mixed-result scenarios, outright market contamination, and ambiguous single-name matching. Each was solved through root-cause analysis rather than workarounds.

### Data Processing
Implemented a multi-stage data pipeline that processes ~1700 raw betting market records per run, filters and deduplicates them across multiple criteria, enriches them via external API calls and LLM processing, and produces structured output in both SQLite (alerts) and JSON (coverage pairs, orphan reports) formats.

### AI/LLM Integration
Used Claude Haiku as a reasoning component within a production pipeline — not as a demo feature. Designed prompts that produce reliable structured output (hex IDs, exact team names), implemented deterministic extraction layers as safety nets around LLM output, built an agent loop with tool use for autonomous multi-step reasoning, and identified precisely where deterministic logic is more appropriate than LLM judgment.

### Software Architecture
Maintained clean separation of concerns across 10+ modules with clear interfaces. Used an atomic write pattern for shared state (`translations.json`) to prevent corruption. Designed for observability with structured logging throughout. Balanced simplicity (SQLite, local execution) against correctness (deduplication, caching, error recovery) without over-engineering.

### Debugging and Iterative Development
Developed through tight feedback loops: run the system against real data, observe discrepancies in match counts or alert quality, trace the issue through logs, identify root cause, implement targeted fix, re-run. Maintained a session log across multiple development days documenting what was built, what bugs were found and fixed, and what final test results confirmed correct behavior.

---

## 8. Resume Extraction Section

### 10 Strong Resume Bullet Points

1. Built an end-to-end sports betting arbitrage system in Python that identifies +EV opportunities by comparing real-time Winner.co.il odds against Pinnacle's sharp lines, sending Telegram alerts when expected value exceeds 4%.

2. Engineered a Playwright-based web scraper with Imperva bot detection bypass using playwright-stealth, maintaining a persistent browser session to avoid repeated fingerprinting challenges during 20-minute polling cycles.

3. Designed and implemented a multi-sport coverage scan using a Claude Haiku agentic loop with three custom tools, discovering Winner↔Pinnacle game mappings across Soccer, Basketball, Tennis, and Baseball without a pre-built league map.

4. Integrated Claude Haiku for Hebrew-to-English team name translation with multi-tier caching (permanent cache for matches, 24-hour cache for NO_MATCH), reducing API calls by 90%+ on repeat runs.

5. Developed a wrong-key detection algorithm using majority-based classification (`no_cand_count > unmatched - no_cand_count`) that signals the autonomous agent to retry with a different Pinnacle sport key, resolving systematic league mismatches.

6. Implemented a single-team fallback matching strategy that resolves ambiguous Hebrew game descriptions by isolating home or away team names and re-querying Claude Haiku with a reduced candidate space, eliminating persistent NO_MATCH failures.

7. Designed a two-track async architecture using `asyncio.gather()` and `asyncio.to_thread()` to run four sport agents in parallel, processing 1700+ market records and completing a full multi-sport scan in under 3 minutes.

8. Applied quantitative finance concepts (vig removal via overround normalization, EV calculation) to translate bookmaker odds into true probabilities, implementing the formula `true_prob = (1/odds) / overround` as the core signal generator.

9. Built a complete data persistence layer with SQLite for alert deduplication, atomic JSON writes via `os.rename()` for shared configuration integrity, and an automated results fetcher that records final match scores against issued alerts.

10. Engineered prompt reliability mechanisms including format-first instruction placement, `max_tokens` right-sizing per task type, and deterministic `re.search(r'\b([0-9a-f]{32})\b')` extraction to handle LLM reasoning-before-answer behavior in production.

---

### Short Project Summary (Resume)

> **EV Betting Bot** — Python | Playwright | Claude Haiku | Async | Telegram | SQLite  
> Automated system that identifies positive expected value betting opportunities by comparing Winner.co.il live odds against Pinnacle's sharp lines in real time. Scraped Winner via Playwright with Imperva bypass; matched games across platforms using Claude Haiku for Hebrew-to-English resolution; built a daily multi-sport coverage scan using an autonomous LLM agent loop. Sends Telegram alerts within seconds of detecting a ≥4% EV edge.

---

### LinkedIn-Style Project Description

**EV Sports Betting Automation System**  
*Python · Playwright · Claude Haiku (Anthropic) · Async · SQLite · Telegram API · The Odds API*

Built a fully automated system for detecting positive expected value betting opportunities across four sports in real time.

The system continuously monitors Winner.co.il (Israel's leading bookmaker) using a Playwright-based scraper with Imperva bot detection bypass, then matches each game against Pinnacle's sharp odds via The Odds API. When Winner's odds exceed Pinnacle's no-vig implied probability by ≥4%, the system sends an instant Telegram alert.

The core challenge was cross-platform game matching: Winner uses Hebrew team names while Pinnacle uses English. Solved this using Claude Haiku with a persistent translation cache, and extended it to a full multi-sport coverage scan using an autonomous Haiku agent loop that discovers all Winner↔Pinnacle game pairs daily without any pre-built league map.

Key engineering work: async parallel sport agents, wrong-key retry logic with majority-based detection, single-team fallback matching for ambiguous Hebrew descriptions, atomic file writes, Pinnacle odds caching, and SQLite-based alert deduplication.

---

### Suggested Project Titles

- **EV Bot — Automated Sports Betting Opportunity Detection System**
- **Cross-Platform Betting Arbitrage System with LLM-Powered Game Matching**
- **Real-Time +EV Sports Alert System (Winner vs Pinnacle)**
- **Multi-Sport Odds Comparison Bot with Hebrew-to-English LLM Matching**
- **Automated Sports Betting Analytics Pipeline with Claude Haiku Agent**

---

## 9. Interview Preparation

### Potential Interview Questions

**Architecture and Design**

*Q: Why did you build a separate coverage scan agent instead of just extending the live EV matcher to more sports?*

A: The live matcher (`matcher.py`) depends on `WINNER_LEAGUE_MAP` — a hand-curated dict mapping Hebrew league names to Pinnacle sport keys. It needs to be fast and reliable during the active betting window, so deterministic lookup is the right tradeoff there. Extending it to more sports would require manually curating hundreds of league mappings and keeping them in sync. The coverage scan agent solves the discovery problem differently: it lets a Claude Haiku agent reason about which leagues correspond to which Pinnacle keys autonomously, using the Pinnacle key's title and sample team names as evidence. The two systems co-exist cleanly — the coverage scan informs future decisions about which leagues to add to the live map, without the live map needing to change.

*Q: How did you handle the LLM being non-deterministic in a production pipeline?*

A: At every point where Claude produces output that will drive downstream logic, I apply a deterministic extraction layer rather than trusting the raw string. For hex IDs: `re.search(r'\b([0-9a-f]{32})\b', raw)` — if no hex is found, fall back to checking for "NO_MATCH" in the text. For team name translation: exact string matching against the known list of Pinnacle team names. For the wrong-key signal: a programmatic majority calculation, not Claude's judgment. The LLM handles the reasoning; the surrounding code handles the output parsing.

*Q: What's the false positive risk in your matching — could you send an alert for the wrong game?*

A: Multiple layers reduce this risk. The ±15-minute kickoff window eliminates most wrong-game candidates before Haiku sees them. The coverage scan produces a debug report (`orphans_{date}.json`) and the Telegram coverage message lists every matched pair as "Hebrew name → Pinnacle English name" — a daily visual sanity check. In the live pipeline, the team name match is exact (not fuzzy) — both home and away must match exactly after translation. The main residual risk is a bad Claude translation that gets cached; this is why the cache-invalidation-on-miss logic exists: if a cached translation fails to produce a game match, it's deleted and re-fetched.

**Technical Deep-Dives**

*Q: Walk me through how the coverage scan agent loop works.*

A: The agent is a standard Claude Haiku tool-use loop. The system prompt gives Haiku the list of Winner leagues (in Hebrew) for a given sport, and access to three tools: `fetch_pinnacle_events` (gets all Pinnacle sport keys with titles and sample teams), `match_games_in_league` (runs game-level matching for a specific Winner league + Pinnacle key pair), and `return_results` (signals done). The agent makes its own decisions about which tool calls to make. Typically it fetches the Pinnacle events first, reasons about which Winner leagues correspond to which keys, then calls `match_games_in_league` for each pairing it believes is correct. If a `match_games_in_league` call comes back with `warning=all_no_candidates`, the system prompt instructs it to retry with a different key for that league.

*Q: How does the Pinnacle probability calculation work and why do you normalize out the overround?*

A: Pinnacle builds a small margin into their odds so that the implied probabilities sum to slightly more than 100% — typically 1.5–3% for major markets. If you use raw implied probabilities (1/odds), you're comparing Winner's odds against a biased number. The overround removal — divide each implied probability by their sum — gives you Pinnacle's best estimate of the true probability. The EV formula is then `(true_prob × winner_odds) - 1`. A result above 0 means the bet has positive expected value; we alert at ≥4% to filter out noise from small pricing discrepancies.

*Q: Why asyncio.to_thread() for the sport agents instead of making the Haiku calls async directly?*

A: The Anthropic Python SDK's `messages.create()` is a synchronous blocking call. Making it async would require using `asyncio.run_in_executor()` or switching to an async HTTP client. `asyncio.to_thread()` is the idiomatic Python 3.9+ way to run a blocking function in a thread pool without blocking the event loop — it achieves true parallelism for I/O-bound work and is simpler than managing executors manually. The four sport agents run simultaneously in separate threads, each making their own Haiku API calls.

**Behavioral / Process**

*Q: Describe a bug you found and how you diagnosed it.*

A: The Haiku token truncation bug is a good example. Five Finnish games had "Haiku returned unknown id" warnings in the log. The raw response showed the 32-char hex ID was missing — the response ended mid-sentence. I hypothesized that Haiku was reasoning before answering, consuming tokens, then getting cut off before it could output the hex ID. I confirmed this by examining other responses where Haiku produced "The Hebrew name translates to... The match is [hex]" — the hex appeared at the end. The fix was: move the format instruction to the prompt's first line (so Haiku constrains its output before reasoning begins) and raise `max_tokens` from 128 to 256. I also added the `re.search()` extraction as a safety net so that any future cases where Haiku adds explanation text after the ID don't break the lookup.

*Q: You mentioned rejecting embeddings. Why, if embeddings are commonly used for semantic search?*

A: I tested them. Adding a multilingual sentence-transformer model improved raw match counts from 11 to 17 out of 87 games. But the LLM-only approach with league filtering matched 20+. The embedding model was 470MB, slow to load, and the multilingual model scored Hebrew queries poorly against English Pinnacle names because Hebrew loan-words are phonetically encoded differently from their English originals. The LLM reasons about phonetics directly from training data — it knows "מנצ'סטר" = "Manchester" because it's seen this in text. Embeddings represent meaning in vector space, not phonetic similarity. For this specific problem, the LLM is the superior tool. I didn't keep embeddings "just in case" — YAGNI.

### Areas Requiring Deeper Study Before Professional Discussion

1. **EV betting regulations**: The legality of cross-bookmaker value betting varies by jurisdiction. Be prepared to discuss this clearly if asked.

2. **Playwright internals**: If pressed on exactly how playwright-stealth works, review which specific browser API properties it patches and why they fingerprint automation.

3. **The Odds API pricing model**: Know the exact quota costs (units per call per market per region) and how the caching strategy maps to specific cost savings in dollar terms.

4. **Claude Haiku cost model**: Know the per-token input/output pricing and roughly how many tokens a typical matching session consumes.

5. **Async Python concurrency model**: Be able to explain the difference between threading, multiprocessing, and asyncio, and specifically why `asyncio.to_thread()` was chosen over alternatives for the Haiku calls.

6. **Statistical validity of EV betting**: Be ready to explain why 4% EV is the threshold, what sample size is needed to confirm edge, and the variance characteristics of sports betting outcomes.

7. **Imperva/bot detection ecosystem**: If asked to go deeper, know the categories of signals WAFs use (TLS fingerprint, JavaScript behavior, timing patterns, IP reputation) and which `playwright-stealth` addresses.

"""
vector_db.py — Daily metadata index for Pinnacle events.

Builds once per day from Pinnacle /events data and persists to disk.
Each Pinnacle event contributes two entries (home + away team), keyed
by role so get_by_time() returns one result per event.

Primary search is time-based: get_by_time() filters by commence_time
proximity, optionally restricted to an Odds API group (e.g. "Soccer").
"""

import json
import logging
import pathlib
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_STORE_DIR = pathlib.Path(__file__).parent / "data" / "vector_db"


class DailyVectorDB:
    """
    In-memory event store for one day's Pinnacle events.

    Typical lifecycle:
        db = DailyVectorDB()
        if not db.load("2026-06-10"):
            db.build(events)
            db.save("2026-06-10")
        results = db.get_by_time(group="Soccer", kickoff=dt)
    """

    def __init__(self) -> None:
        self._metadata: list[dict] = []

    # ── Build ─────────────────────────────────────────────────────────────────

    def build(self, events: list[dict]) -> None:
        """
        Index all team entries from a list of Pinnacle events.

        Each event produces two entries (home + away). Metadata stored per entry:
            event_id, sport_key, group, home_team, away_team, commence_time,
            team (the team name for this entry), role ("home" | "away")
        """
        entries: list[dict] = []
        for ev in events:
            for role, team in [("home", ev["home_team"]), ("away", ev["away_team"])]:
                if team:
                    entries.append({
                        "event_id":      ev["id"],
                        "sport_key":     ev["sport_key"],
                        "group":         ev["group"],
                        "home_team":     ev["home_team"],
                        "away_team":     ev["away_team"],
                        "commence_time": ev["commence_time"],
                        "team":          team,
                        "role":          role,
                    })

        if not entries:
            log.warning("[VectorDB] build() called with 0 events — index is empty.")
            return

        self._metadata = entries
        log.info("[VectorDB] Built index — %d entries", len(entries))

    # ── Persist ───────────────────────────────────────────────────────────────

    def save(self, date_str: str) -> None:
        """Persist metadata to data/vector_db/{date_str}.json"""
        if not self._metadata:
            log.error("[VectorDB] Nothing to save — build() first.")
            return

        _STORE_DIR.mkdir(parents=True, exist_ok=True)
        with open(_STORE_DIR / f"{date_str}.json", "w", encoding="utf-8") as f:
            json.dump(self._metadata, f, ensure_ascii=False, indent=None)

        log.info("[VectorDB] Saved %d entries for %s", len(self._metadata), date_str)

    def load(self, date_str: str) -> bool:
        """
        Load previously saved metadata from disk.
        Returns True if the file exists and loads successfully.
        """
        json_path = _STORE_DIR / f"{date_str}.json"

        if not json_path.exists():
            log.info("[VectorDB] No saved index for %s", date_str)
            return False

        try:
            with open(json_path, encoding="utf-8") as f:
                self._metadata = json.load(f)
        except Exception as exc:
            log.error("[VectorDB] Failed to load index for %s: %s", date_str, exc)
            return False

        log.info("[VectorDB] Loaded %d entries for %s", len(self._metadata), date_str)
        return True

    # ── Search ────────────────────────────────────────────────────────────────

    def get_by_time(
        self, group: str | None, kickoff: datetime, window_s: int = 900
    ) -> list[dict]:
        """
        Return all home-role Pinnacle entries that fall within window_s seconds
        of kickoff. When group is given, restricts to that Odds API sport group;
        when None, searches across all groups.

        Returns one dict per Pinnacle event (role="home" entries only, so each
        game appears once). Each dict has the full event metadata.
        """
        results = []
        for m in self._metadata:
            if m["role"] != "home":
                continue
            if group is not None and m["group"] != group:
                continue
            try:
                ct = datetime.fromisoformat(m["commence_time"])
                if ct.tzinfo is None:
                    ct = ct.replace(tzinfo=timezone.utc)
                if abs((ct - kickoff).total_seconds()) <= window_s:
                    results.append(m)
            except ValueError:
                continue
        return results

"""
database.py — SQLite database for tracking EV alerts and their results.
"""
import logging
import sqlite3
import pathlib
from datetime import datetime, timedelta, timezone

_DB_PATH = pathlib.Path(__file__).parent / "alerts.db"

log = logging.getLogger(__name__)

_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS alerts (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id    INTEGER NOT NULL,
        home        TEXT NOT NULL,
        away        TEXT NOT NULL,
        kickoff     TEXT NOT NULL,
        outcome     TEXT NOT NULL,
        winner_odds REAL NOT NULL,
        fair_odds   REAL NOT NULL,
        ev_pct      REAL NOT NULL,
        result      TEXT,
        created_at  TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (event_id, outcome, ev_pct)
    )
"""

_MIGRATION_DDL = """
    CREATE TABLE alerts_new (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id    INTEGER NOT NULL,
        home        TEXT NOT NULL,
        away        TEXT NOT NULL,
        kickoff     TEXT NOT NULL,
        outcome     TEXT NOT NULL,
        winner_odds REAL NOT NULL,
        fair_odds   REAL NOT NULL,
        ev_pct      REAL NOT NULL,
        result      TEXT,
        created_at  TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (event_id, outcome, ev_pct)
    );
    INSERT OR IGNORE INTO alerts_new
        SELECT id, event_id, home, away, kickoff, outcome,
               winner_odds, fair_odds, ev_pct, result, created_at
        FROM alerts;
    DROP TABLE alerts;
    ALTER TABLE alerts_new RENAME TO alerts;
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _has_correct_constraint(conn: sqlite3.Connection) -> bool:
    """Return True if the alerts table has the UNIQUE(event_id, outcome, ev_pct) constraint."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='alerts'"
    ).fetchone()
    return row is not None and "outcome, ev_pct" in row[0]


def init_db() -> None:
    """Create the alerts table (with UNIQUE constraint), migrating if needed."""
    with _connect() as conn:
        conn.execute(_TABLE_DDL)
        if not _has_correct_constraint(conn):
            log.info("[DB] Migrating alerts table to UNIQUE(event_id, outcome, ev_pct)...")
            conn.executescript(_MIGRATION_DDL)
            log.info("[DB] Migration complete.")


def save_alert(alert: dict, event_id: int) -> None:
    """Insert a new alert row. Silently ignores duplicates."""
    home, away = alert["match"].split(" vs ", 1)
    with _connect() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO alerts
                (event_id, home, away, kickoff, outcome, winner_odds, fair_odds, ev_pct)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                home,
                away,
                alert["kickoff"],
                alert["outcome"],
                alert["winner_odds"],
                alert["pinnacle_fair_odds"],
                alert["ev_pct"],
            ),
        )


def get_pending_results() -> list[dict]:
    """Return all rows where result IS NULL and kickoff was more than 180 minutes ago."""
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(minutes=180)).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM alerts
            WHERE result IS NULL
              AND kickoff <= ?
            """,
            (cutoff,),
        ).fetchall()
    return [dict(row) for row in rows]


def update_result(alert_id: int, result: str) -> None:
    """Set the result for an alert row."""
    with _connect() as conn:
        conn.execute(
            "UPDATE alerts SET result = ? WHERE id = ?",
            (result, alert_id),
        )


def alert_exists(event_id: int, outcome: str, ev_pct: float) -> bool:
    """Check if an alert for this event+outcome+ev_pct has already been saved."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM alerts WHERE event_id = ? AND outcome = ? AND ev_pct = ?",
            (event_id, outcome, ev_pct),
        ).fetchone()
    return row is not None

"""
database.py — SQLite storage for EV alerts and match results.
"""
import pathlib
import sqlite3
from datetime import datetime, timedelta, timezone

_DB_PATH = pathlib.Path(__file__).parent / "alerts.db"

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER NOT NULL,
    home        TEXT    NOT NULL,
    away        TEXT    NOT NULL,
    kickoff     TEXT    NOT NULL,
    outcome     TEXT    NOT NULL,
    winner_odds REAL    NOT NULL,
    fair_odds   REAL    NOT NULL,
    ev_pct      REAL    NOT NULL,
    result      TEXT,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(event_id, outcome, ev_pct)
)
"""

_MIGRATION_DDL = """
ALTER TABLE alerts RENAME TO alerts_old;

CREATE TABLE alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER NOT NULL,
    home        TEXT    NOT NULL,
    away        TEXT    NOT NULL,
    kickoff     TEXT    NOT NULL,
    outcome     TEXT    NOT NULL,
    winner_odds REAL    NOT NULL,
    fair_odds   REAL    NOT NULL,
    ev_pct      REAL    NOT NULL,
    result      TEXT,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(event_id, outcome, ev_pct)
);

INSERT INTO alerts
    SELECT id, event_id, home, away, kickoff, outcome,
           winner_odds, fair_odds, ev_pct, result, created_at
    FROM alerts_old;

DROP TABLE alerts_old;
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _has_correct_constraint(conn: sqlite3.Connection) -> bool:
    schema = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='alerts'"
    ).fetchone()
    return schema is not None and "outcome, ev_pct" in schema["sql"]


def init_db() -> None:
    with _connect() as conn:
        conn.execute(_TABLE_DDL)
        if not _has_correct_constraint(conn):
            conn.executescript(_MIGRATION_DDL)


def save_alert(alert: dict, event_id: int) -> None:
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
                alert.get("kickoff", ""),
                alert["outcome"],
                alert["winner_odds"],
                alert["pinnacle_fair_odds"],
                alert["ev_pct"],
            ),
        )


def alert_exists(event_id: int, outcome: str, ev_pct: float) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM alerts WHERE event_id=? AND outcome=? AND ev_pct=?",
            (event_id, outcome, ev_pct),
        ).fetchone()
    return row is not None


def get_pending_results() -> list[dict]:
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(minutes=180)).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, event_id, home, away, kickoff, outcome
            FROM alerts
            WHERE result IS NULL AND kickoff <= ?
            """,
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def update_result(alert_id: int, result: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE alerts SET result=? WHERE id=?",
            (result, alert_id),
        )

# /test-full-pipeline

Run a full end-to-end test of the EV pipeline using a fake match designed to produce 4%+ EV.
Tests: EV calculation, database deduplication, Telegram sending, and DB cleanup.
Production code must not be modified — use a temporary script.

## Steps

1. Write a temporary Python script `_test_full_pipeline_run.py` in the project root with the following logic:

```python
import asyncio, sys, sqlite3
sys.stdout.reconfigure(encoding="utf-8")

import logging
logging.basicConfig(level=logging.INFO, format="%(message)s")

from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv
load_dotenv()

from ev_calculator import calculate_ev
from database import init_db, save_alert, alert_exists, update_result
from telegram_bot import send_alerts

init_db()

def now_plus(minutes: int) -> str:
    return (datetime.now(tz=timezone.utc) + timedelta(minutes=minutes)).isoformat()

# ── Fake matched pair ──────────────────────────────────────────────────────────
EVENT_ID = 99999
kickoff  = now_plus(30)

def make_pair(home_odds: float) -> dict:
    return {
        "winner": {
            "event_id":    EVENT_ID,
            "description": "ליברפול - ארסנל",
            "kickoff":     kickoff,
            "outcomes": [
                {"desc": "1",  "price": home_odds},
                {"desc": "\u202BX\u202C", "price": 3.80},
                {"desc": "2",  "price": 2.90},
            ],
        },
        "pinnacle": {
            "home_team":     "Liverpool",
            "away_team":     "Arsenal",
            "home_odds":     2.10,
            "draw_odds":     3.50,
            "away_odds":     3.20,
            "commence_time": kickoff,
        },
    }

def pass_fail(condition: bool, label: str) -> None:
    print(f"  {'PASS' if condition else 'FAIL'} — {label}")
    if not condition:
        raise SystemExit(1)

print("\n" + "=" * 60)
print("Full Pipeline Test")
print("=" * 60)

# ── STEP 2: EV calculation ─────────────────────────────────────────────────────
print("\n[Step 2] EV Calculation")
pair  = make_pair(home_odds=2.50)
alerts = calculate_ev([pair])
home_alerts = [a for a in alerts if a["outcome"] == "Home"]
pass_fail(len(home_alerts) >= 1, f"at least one Home alert generated (got {len(home_alerts)})")
alert = home_alerts[0]
pass_fail(alert["ev_pct"] >= 4.0, f"EV >= 4% (got {alert['ev_pct']}%)")
print(f"  Alert: {alert['match']}  Home  EV=+{alert['ev_pct']}%")

# ── STEP 3: First send ─────────────────────────────────────────────────────────
print("\n[Step 3] First Send")
exists_before = alert_exists(EVENT_ID, alert["outcome"], alert["ev_pct"])
pass_fail(not exists_before, "alert_exists() → False before first save")
save_alert(alert, EVENT_ID)
asyncio.run(send_alerts([alert]))
exists_after = alert_exists(EVENT_ID, alert["outcome"], alert["ev_pct"])
pass_fail(exists_after, "alert_exists() → True after save")

# ── STEP 4: Duplicate test ─────────────────────────────────────────────────────
print("\n[Step 4] Duplicate Test (same EV)")
already_exists = alert_exists(EVENT_ID, alert["outcome"], alert["ev_pct"])
pass_fail(already_exists, "alert_exists() → True, alert skipped")
print("  (No second Telegram message sent)")

# ── STEP 5: Better EV test ─────────────────────────────────────────────────────
print("\n[Step 5] Better EV Test (higher winner odds)")
pair2   = make_pair(home_odds=2.70)
alerts2 = calculate_ev([pair2])
home2   = [a for a in alerts2 if a["outcome"] == "Home"][0]
print(f"  New alert: EV=+{home2['ev_pct']}%")
new_is_different = home2["ev_pct"] != alert["ev_pct"]
pass_fail(new_is_different, f"new ev_pct ({home2['ev_pct']}) differs from original ({alert['ev_pct']})")
exists_new = alert_exists(EVENT_ID, home2["outcome"], home2["ev_pct"])
pass_fail(not exists_new, "alert_exists() → False for new ev_pct")
save_alert(home2, EVENT_ID)
asyncio.run(send_alerts([home2]))
exists_new_after = alert_exists(EVENT_ID, home2["outcome"], home2["ev_pct"])
pass_fail(exists_new_after, "alert_exists() → True after second save")

import pathlib, sqlite3 as _sq
db = _sq.connect(pathlib.Path("alerts.db"))
rows = db.execute(
    "SELECT COUNT(*) FROM alerts WHERE event_id=? AND outcome='Home'", (EVENT_ID,)
).fetchone()[0]
db.close()
pass_fail(rows == 2, f"2 rows in DB for event_id={EVENT_ID}, outcome=Home (got {rows})")

# ── STEP 6: Cleanup ────────────────────────────────────────────────────────────
print("\n[Step 6] Cleanup")
db = _sq.connect(pathlib.Path("alerts.db"))
deleted = db.execute("DELETE FROM alerts WHERE event_id=?", (EVENT_ID,)).rowcount
db.commit()
db.close()
pass_fail(deleted == 2, f"deleted 2 rows (got {deleted})")

print("\n" + "=" * 60)
print("All steps PASSED.")
print("=" * 60)
```

2. Run the script using the project virtualenv:
   ```
   .venv/Scripts/python _test_full_pipeline_run.py
   ```

3. Print all output to the user.

4. Delete `_test_full_pipeline_run.py` after the run completes (whether it succeeded or failed).

5. Do not edit any production files.

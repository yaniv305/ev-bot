"""
ev_calculator.py — Calculate Expected Value for Winner vs Pinnacle matched pairs.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

_ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")
_MIN_EV = 0.04  # 4% minimum EV threshold

_RTL_STRIP = "\u202B\u202C"  # RIGHT-TO-LEFT EMBEDDING / POP DIRECTIONAL FORMATTING


def calculate_ev(matched_pairs: list[dict]) -> list[dict]:
    """
    Calculate EV for each 1X2 outcome in each matched pair.

    Args:
        matched_pairs: list of {"winner": dict, "pinnacle": dict} from match_markets().

    Returns:
        List of alert dicts for outcomes where EV >= 4%, sorted by ev_pct descending.
    """
    alerts = []

    for pair in matched_pairs:
        wm = pair["winner"]
        pm = pair["pinnacle"]

        # Pinnacle implied probs → remove overround → true probs
        h, d, a = pm["home_odds"], pm["draw_odds"], pm["away_odds"]
        if not all([h, d, a]):
            continue
        overround = 1/h + 1/d + 1/a
        true_home = (1/h) / overround
        true_draw = (1/d) / overround
        true_away = (1/a) / overround

        # Winner odds: draw identified by exact "X" after stripping RTL markers
        w_home = w_draw = w_away = None
        non_draw = []
        for o in wm.get("outcomes", []):
            if o["desc"].strip(_RTL_STRIP) == "X":
                w_draw = o["price"]
            else:
                non_draw.append(o["price"])
        if len(non_draw) >= 2:
            w_home, w_away = non_draw[0], non_draw[1]

        # Kickoff → Israel time
        try:
            israel_time = (
                datetime.fromisoformat(wm["kickoff"])
                .astimezone(_ISRAEL_TZ)
                .strftime("%H:%M")
            )
        except (ValueError, KeyError):
            israel_time = "?"

        match_name = f"{pm['home_team']} vs {pm['away_team']}"

        for outcome, true_prob, w_odds in [
            ("Home", true_home, w_home),
            ("Draw", true_draw, w_draw),
            ("Away", true_away, w_away),
        ]:
            if w_odds is None:
                continue
            ev = (true_prob * w_odds) - 1
            if ev >= _MIN_EV:
                alerts.append({
                    "match": match_name,
                    "israel_time": israel_time,
                    "kickoff": wm["kickoff"],
                    "market": "1X2",
                    "outcome": outcome,
                    "winner_odds": w_odds,
                    "pinnacle_fair_odds": round(1 / true_prob, 2),
                    "ev_pct": round(ev * 100, 1),
                })

    alerts.sort(key=lambda x: x["ev_pct"], reverse=True)
    return alerts

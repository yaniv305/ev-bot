"""
ev_calculator.py — Calculate Expected Value for Winner vs Pinnacle matched pairs.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

_ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")
_MIN_EV = 0.04  # 4% minimum EV threshold

_RTL_STRIP = "\u202B\u202C"  # RIGHT-TO-LEFT EMBEDDING / POP DIRECTIONAL FORMATTING


def _israel_time(kickoff_str: str) -> str:
    try:
        return (
            datetime.fromisoformat(kickoff_str)
            .astimezone(_ISRAEL_TZ)
            .strftime("%H:%M")
        )
    except (ValueError, KeyError):
        return "?"


def calculate_ev(matched_pairs: list[dict]) -> list[dict]:
    """
    Calculate EV for each matched pair.

    Pair shapes:
      {"winner": dict, "pinnacle": dict, "market": "h2h"}     — 1X2
      {"winner": dict, "pinnacle": dict, "market": "totals"}  — Over/Under goals
      {"winner": dict, "pinnacle": dict, "market": "btts"}    — Both Teams To Score

    For backward compatibility, pairs without a "market" key are treated as "h2h".

    Returns:
        List of alert dicts for outcomes where EV >= 4%, sorted by ev_pct descending.
    """
    alerts = []

    for pair in matched_pairs:
        wm     = pair["winner"]
        pm     = pair["pinnacle"]
        market = pair.get("market", "h2h")

        israel_time    = _israel_time(wm.get("kickoff", ""))
        winner_event_id = wm.get("event_id")

        if market == "h2h":
            h, d, a = pm["home_odds"], pm["draw_odds"], pm["away_odds"]
            if not all([h, d, a]):
                continue
            overround = 1/h + 1/d + 1/a
            true_home = (1/h) / overround
            true_draw = (1/d) / overround
            true_away = (1/a) / overround

            match_name = f"{pm['home_team']} vs {pm['away_team']}"

            w_home = w_draw = w_away = None
            non_draw = []
            for o in wm.get("outcomes", []):
                if o["desc"].strip(_RTL_STRIP) == "X":
                    w_draw = o["price"]
                else:
                    non_draw.append(o["price"])
            if len(non_draw) >= 2:
                w_home, w_away = non_draw[0], non_draw[1]

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
                        "match":              match_name,
                        "israel_time":        israel_time,
                        "kickoff":            wm.get("kickoff", ""),
                        "market":             "1X2",
                        "outcome":            outcome,
                        "winner_odds":        w_odds,
                        "pinnacle_fair_odds": round(1 / true_prob, 2),
                        "ev_pct":             round(ev * 100, 1),
                        "winner_event_id":    winner_event_id,
                    })

        elif market == "totals":
            over_odds  = pm.get("over_odds")
            under_odds = pm.get("under_odds")
            point      = pm.get("point")
            if not over_odds or not under_odds or point is None:
                continue
            overround  = 1/over_odds + 1/under_odds
            true_over  = (1/over_odds)  / overround
            true_under = (1/under_odds) / overround

            match_name = f"{pm['home_team']} vs {pm['away_team']}"

            w_over = w_under = None
            for o in wm.get("outcomes", []):
                desc = o["desc"].strip(_RTL_STRIP)
                if desc.startswith("מעל"):
                    w_over = o["price"]
                elif desc.startswith("מתחת"):
                    w_under = o["price"]

            for outcome, true_prob, w_odds in [
                ("Over",  true_over,  w_over),
                ("Under", true_under, w_under),
            ]:
                if w_odds is None:
                    continue
                ev = (true_prob * w_odds) - 1
                if ev >= _MIN_EV:
                    alerts.append({
                        "match":              match_name,
                        "israel_time":        israel_time,
                        "kickoff":            wm.get("kickoff", ""),
                        "market":             f"O/U {point}",
                        "outcome":            outcome,
                        "winner_odds":        w_odds,
                        "pinnacle_fair_odds": round(1 / true_prob, 2),
                        "ev_pct":             round(ev * 100, 1),
                        "winner_event_id":    winner_event_id,
                    })

        elif market == "btts":
            yes_odds = pm.get("btts_yes_odds")
            no_odds  = pm.get("btts_no_odds")
            if not yes_odds or not no_odds:
                continue
            overround = 1/yes_odds + 1/no_odds
            true_yes  = (1/yes_odds) / overround
            true_no   = (1/no_odds)  / overround

            match_name = f"{pm['home_team']} vs {pm['away_team']}"

            w_yes = w_no = None
            for o in wm.get("outcomes", []):
                desc = o["desc"].strip(_RTL_STRIP)
                if desc == "כן":
                    w_yes = o["price"]
                elif desc == "לא":
                    w_no = o["price"]

            for outcome, true_prob, w_odds in [
                ("Yes", true_yes, w_yes),
                ("No",  true_no,  w_no),
            ]:
                if w_odds is None:
                    continue
                ev = (true_prob * w_odds) - 1
                if ev >= _MIN_EV:
                    alerts.append({
                        "match":              match_name,
                        "israel_time":        israel_time,
                        "kickoff":            wm.get("kickoff", ""),
                        "market":             "BTTS",
                        "outcome":            outcome,
                        "winner_odds":        w_odds,
                        "pinnacle_fair_odds": round(1 / true_prob, 2),
                        "ev_pct":             round(ev * 100, 1),
                        "winner_event_id":    winner_event_id,
                    })

    alerts.sort(key=lambda x: x["ev_pct"], reverse=True)
    return alerts

import pytest
from ev_calculator import calculate_ev

_RTL_X = "\u202BX\u202C"


def _make_pair(pin_home, pin_draw, pin_away, win_home, win_draw, win_away):
    return {
        "winner": {
            "description": "Team A - Team B",
            "kickoff": "2026-05-03T16:00:00+00:00",
            "outcomes": [
                {"desc": "1",    "price": win_home},
                {"desc": _RTL_X, "price": win_draw},
                {"desc": "2",    "price": win_away},
            ],
        },
        "pinnacle": {
            "home_team": "Team A",
            "away_team": "Team B",
            "home_odds": pin_home,
            "draw_odds": pin_draw,
            "away_odds": pin_away,
            "commence_time": "2026-05-03T16:00:00Z",
        },
    }


@pytest.mark.parametrize("pair, expected_outcomes", [
    pytest.param(
        _make_pair(2.0, 3.5, 4.0,  2.50, 3.5, 4.0),
        ["Home"],
        id="home_clear_ev",
    ),
    pytest.param(
        _make_pair(2.0, 3.5, 4.0,  2.08, 3.5, 4.0),
        [],
        id="no_ev_below_threshold",
    ),
    pytest.param(
        _make_pair(1.5, 4.0, 6.0,  1.5, 5.0, 6.0),
        ["Draw"],
        id="draw_ev_only",
    ),
    pytest.param(
        _make_pair(1.5, None, 2.5,  2.0, 3.8, 3.0),
        [],
        id="pinnacle_no_draw_odds_skips_pair",
    ),
])
def test_ev_outcomes(pair, expected_outcomes):
    alerts = calculate_ev([pair])
    assert [a["outcome"] for a in alerts] == expected_outcomes


def test_ev_alert_fields():
    """Alert dict contains all expected keys with correct types."""
    pair = _make_pair(2.0, 3.5, 4.0,  2.50, 3.5, 4.0)
    alerts = calculate_ev([pair])
    assert len(alerts) == 1
    a = alerts[0]
    assert a["match"] == "Team A vs Team B"
    assert a["outcome"] == "Home"
    assert a["ev_pct"] >= 4.0
    assert a["winner_odds"] == 2.50
    assert "kickoff" in a
    assert "israel_time" in a
    assert "pinnacle_fair_odds" in a


def test_ev_sorted_descending():
    """Multiple alerts come back sorted by ev_pct, highest first."""
    pair = _make_pair(2.0, 3.5, 4.0,  2.50, 4.5, 5.5)
    alerts = calculate_ev([pair])
    assert len(alerts) >= 2
    ev_values = [a["ev_pct"] for a in alerts]
    assert ev_values == sorted(ev_values, reverse=True)

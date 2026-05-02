# EV Bot — Playbook
Last updated: 2026-04-28

## Matching Conditions
These conditions must ALL pass for a Winner game to be matched against a Pinnacle game.
Update this file whenever a condition changes.

1. Same league — Hebrew league name maps to Pinnacle sport key via translations.json
2. Winner window — kickoff within 30 min from now (filtered in winner_scraper.py)
3. Pinnacle window — kickoff within 45 min from now (filtered in matcher.py)
4. Kickoff proximity — Winner vs Pinnacle kickoff difference ≤ 15 min
5. Team name match — exact or fuzzy (90%+) after Hebrew→English translation

## Markets
Currently supported: 1X2 only
Planned: Totals, European Handicap, Goal Range

## Leagues
Mapped in translations.json — 33 leagues currently supported.

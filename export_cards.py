#!/usr/bin/env python3
"""Print approved katas and bug exercises as JSON cards for the web app.

Used by web/scripts/sync-cards.ts (npm run cards). Pending candidates are never exported.
"""
import json

import bugs
import kata


def exercise_cards() -> list[dict]:
    cards = [kata.card_for(m, kata.KATAS / i) for i, m in sorted(kata.load_katas().items())]
    cards += [bugs.card_for(m, bugs.BUGS / i) for i, m in sorted(bugs.load_bugs().items())]
    return cards


if __name__ == "__main__":
    print(json.dumps(exercise_cards(), indent=2))

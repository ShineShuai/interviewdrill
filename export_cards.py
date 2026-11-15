#!/usr/bin/env python3
"""Export approved katas and bug exercises for the web app.

  export_cards.py                 print them as JSON cards (web/scripts/sync-cards.ts)
  export_cards.py --tests DIR     write DIR/<kata-id>.json with the tests for the browser
                                  runner; stale files in DIR are removed
Pending candidates are never exported.
"""
import argparse
import json
from pathlib import Path

import bugs
import kata


def exercise_cards() -> list[dict]:
    cards = [kata.card_for(m, kata.KATAS / i) for i, m in sorted(kata.load_katas().items())]
    cards += [bugs.card_for(m, bugs.BUGS / i) for i, m in sorted(bugs.load_bugs().items())]
    return cards


def export_tests(out: Path) -> list[str]:
    """Write one JSON file per approved kata; returns the kata ids."""
    out.mkdir(parents=True, exist_ok=True)
    ids = sorted(kata.load_katas())
    for kid in ids:
        (out / f"{kid}.json").write_text(json.dumps(kata.tests_payload(kata.KATAS / kid)))
    for stale in out.glob("*.json"):
        if stale.stem not in ids:
            stale.unlink()
    return ids


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tests", type=Path, metavar="DIR")
    args = ap.parse_args()
    if args.tests:
        print(f"wrote tests for {len(export_tests(args.tests))} katas to {args.tests}")
    else:
        print(json.dumps(exercise_cards(), indent=2))

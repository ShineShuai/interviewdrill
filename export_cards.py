#!/usr/bin/env python3
"""Export approved problems and bug exercises for the web app.

  export_cards.py                 print them as JSON cards (web/scripts/sync-cards.ts)
  export_cards.py --tests DIR     write DIR/<problem-id>.json with the tests for the browser
                                  runner; stale files in DIR are removed
Pending candidates are never exported.
"""
import argparse
import json
from pathlib import Path

import bugs
import problems


def exercise_cards() -> list[dict]:
    found = sorted(problems.load_problems().items())
    cards = [problems.card_for(m, problems.PROBLEMS / i) for i, m in found]
    cards += [bugs.card_for(m, bugs.BUGS / i) for i, m in sorted(bugs.load_bugs().items())]
    return cards


def export_tests(out: Path) -> list[str]:
    """Write one JSON file per approved problem; returns the problem ids."""
    out.mkdir(parents=True, exist_ok=True)
    ids = sorted(problems.load_problems())
    for kid in ids:
        payload = problems.tests_payload(problems.PROBLEMS / kid)
        (out / f"{kid}.json").write_text(json.dumps(payload))
    for stale in out.glob("*.json"):
        if stale.stem not in ids:
            stale.unlink()
    return ids


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tests", type=Path, metavar="DIR")
    args = ap.parse_args()
    if args.tests:
        print(f"wrote tests for {len(export_tests(args.tests))} problems to {args.tests}")
    else:
        print(json.dumps(exercise_cards(), indent=2))

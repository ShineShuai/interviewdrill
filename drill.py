#!/usr/bin/env python3
"""drill: spaced-repetition interview practice with LLM rubric grading.

Usage: drill.py daily [-n 5] [-t topic] [--speak] | drill.py report [-d 14]
Env: ANTHROPIC_API_KEY, DRILL_HOME (~/.drill), DRILL_CARDS, DRILL_MODEL,
     DRILL_STT (shell command that records and prints a transcript).
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path(os.environ.get("DRILL_HOME", "~/.drill")).expanduser()
CARDS = Path(os.environ.get("DRILL_CARDS", Path(__file__).parent / "cards"))
MODEL = os.environ.get("DRILL_MODEL", "claude-sonnet-5-5")
STT = os.environ.get("DRILL_STT")
WEIGHTS = {"correct": 0.5, "depth": 0.3, "clarity": 0.2}

SYSTEM = """You are a strict technical interviewer grading one answer.
Grade only against the reference points and what the candidate actually said.
No credit for implied knowledge. Quote the candidate for every error.
Scores are integers 0-5. Be terse."""

GRADE_TOOL = {
    "name": "grade",
    "description": "Record the grade for the candidate's answer.",
    "input_schema": {
        "type": "object",
        "properties": {
            "correct": {"type": "integer", "minimum": 0, "maximum": 5},
            "depth": {"type": "integer", "minimum": 0, "maximum": 5},
            "clarity": {"type": "integer", "minimum": 0, "maximum": 5},
            "missed": {"type": "array", "items": {"type": "string"}},
            "errors": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"quote": {"type": "string"}, "why": {"type": "string"}},
                    "required": ["quote", "why"],
                },
            },
            "followup": {"type": "string"},
        },
        "required": ["correct", "depth", "clarity", "missed", "errors", "followup"],
    },
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards(
  id TEXT PRIMARY KEY, topic TEXT, question TEXT, ref TEXT,
  ease REAL DEFAULT 2.5, interval REAL DEFAULT 0, reps INTEGER DEFAULT 0,
  due TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS attempts(
  id INTEGER PRIMARY KEY, card_id TEXT, ts TEXT, secs REAL, answer TEXT,
  correct INTEGER, depth INTEGER, clarity INTEGER, score REAL, detail TEXT);
"""


def now(delta_days: float = 0.0) -> str:
    t = datetime.now(timezone.utc) + timedelta(days=delta_days)
    return t.isoformat(timespec="seconds")


def load_cards(path: Path | None = None) -> list[dict]:
    """Parse `## id | question` headings; body lines minus <!-- --> comments are the reference."""
    cards = []
    for file in sorted((path or CARDS).glob("*.md")):
        for block in ("\n" + file.read_text()).split("\n## ")[1:]:
            head, _, body = block.partition("\n")
            cid, _, question = head.partition("|")
            ref = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("<!--"))
            cards.append({"id": cid.strip(), "topic": file.stem,
                          "question": question.strip(), "ref": ref.strip()})
    return cards


def connect() -> sqlite3.Connection:
    HOME.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(HOME / "drill.db")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    cards = load_cards()
    for c in cards:
        db.execute(
            "INSERT INTO cards(id, topic, question, ref) VALUES(?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET topic=excluded.topic, "
            "question=excluded.question, ref=excluded.ref",
            (c["id"], c["topic"], c["question"], c["ref"]),
        )
    ids = json.dumps([c["id"] for c in cards])
    db.execute("DELETE FROM cards WHERE id NOT IN (SELECT value FROM json_each(?))", (ids,))
    db.commit()
    return db


def schedule(ease: float, interval: float, reps: int, q: float) -> tuple[float, float, int]:
    """SM-2 variant; q in 0..5. Returns (ease, interval_days, reps)."""
    if q < 3:
        return max(1.3, ease - 0.2), 1.0, 0
    ease = max(1.3, ease + 0.1 - (5 - q) * (0.08 + (5 - q) * 0.02))
    interval = (1.0, 3.0)[reps] if reps < 2 else interval * ease
    return ease, interval, reps + 1


def pick(db: sqlite3.Connection, n: int, topic: str | None) -> list[sqlite3.Row]:
    sql, args = "SELECT * FROM cards WHERE due <= ?", [now()]
    if topic:
        sql, args = sql + " AND topic = ?", args + [topic]
    return db.execute(sql + " ORDER BY due, RANDOM() LIMIT ?", args + [n]).fetchall()


def read_answer() -> str:
    if STT:
        out = subprocess.run(STT, shell=True, capture_output=True, text=True, check=True)
        return out.stdout.strip()
    print("(answer; finish with an empty line)")
    lines = []
    while (line := sys.stdin.readline()).strip():
        lines.append(line.rstrip())
    return "\n".join(lines)


HINTS = {
    "bug:": "This is a debugging exercise. The reference is verified ground truth (a sanitizer, "
            "race detector or runtime report, and a minimal fix). 'correct' = names the bug "
            "class and root cause; 'depth' = a valid fix and why it works (any correct fix "
            "counts); 'clarity' = communication.",
    "problem:": "This is a coding exercise answered as an approach, pseudocode or code. The "
             "reference solution is verified. 'correct' = would give right answers within the "
             "limits (equal or better algorithms count); 'depth' = complexity and edge cases; "
             "'clarity' = communication.",
}


def system_for(card_id: str) -> str:
    return "\n".join([SYSTEM, *(h for p, h in HINTS.items() if card_id.startswith(p))])


def judge(card: sqlite3.Row, answer: str) -> dict:
    import anthropic  # lazy: offline commands and tests need no SDK

    prompt = (f"Question: {card['question']}\n\nReference points:\n{card['ref']}\n\n"
              f"Candidate answer:\n{answer or '(no answer)'}")
    msg = anthropic.Anthropic().messages.create(
        model=MODEL, max_tokens=1000, system=system_for(card["id"]), tools=[GRADE_TOOL],
        tool_choice={"type": "tool", "name": "grade"},
        messages=[{"role": "user", "content": prompt}],
    )
    return next(b.input for b in msg.content if b.type == "tool_use")


def score(g: dict) -> float:
    return sum(g[k] * w for k, w in WEIGHTS.items())


def show(g: dict, q: float, interval: float) -> None:
    print(f"score {q:.1f}/5 (correct {g['correct']}, depth {g['depth']}, "
          f"clarity {g['clarity']}) -> next in {interval:.0f}d")
    for m in g["missed"]:
        print(f"  missed: {m}")
    for e in g["errors"]:
        print(f"  error: \"{e['quote']}\" - {e['why']}")
    print(f"  follow-up: {g['followup']}")


def cmd_daily(args: argparse.Namespace) -> None:
    db = connect()
    cards = pick(db, args.n, args.topic)
    if not cards:
        print("nothing due")
        return
    for card in cards:
        print(f"\n[{card['topic']}] {card['question']}")
        if args.speak:
            subprocess.run(["say", card["question"]], check=False)
        t0 = time.monotonic()
        answer = read_answer()
        secs = time.monotonic() - t0
        g = judge(card, answer)
        q = score(g)
        ease, interval, reps = schedule(card["ease"], card["interval"], card["reps"], q)
        db.execute("UPDATE cards SET ease=?, interval=?, reps=?, due=? WHERE id=?",
                   (ease, interval, reps, now(interval), card["id"]))
        db.execute(
            "INSERT INTO attempts(card_id, ts, secs, answer, correct, depth, clarity,"
            " score, detail) VALUES(?,?,?,?,?,?,?,?,?)",
            (card["id"], now(), secs, answer, g["correct"], g["depth"], g["clarity"],
             q, json.dumps(g)),
        )
        db.commit()
        show(g, q, interval)


def cmd_report(args: argparse.Namespace) -> None:
    db = connect()
    since = now(-args.days)
    print(f"{'topic':<12}{'n':>4}{'score':>7}{'corr':>7}{'depth':>7}{'clar':>7}")
    rows = db.execute(
        "SELECT c.topic t, COUNT(*) n, AVG(a.score) s, AVG(a.correct) co,"
        " AVG(a.depth) d, AVG(a.clarity) cl FROM attempts a JOIN cards c"
        " ON c.id = a.card_id WHERE a.ts >= ? GROUP BY c.topic ORDER BY s", (since,))
    for r in rows:
        print(f"{r['t']:<12}{r['n']:>4}{r['s']:>7.1f}{r['co']:>7.1f}{r['d']:>7.1f}{r['cl']:>7.1f}")
    print("\nweakest cards:")
    rows = db.execute(
        "SELECT card_id, AVG(score) s FROM attempts WHERE ts >= ?"
        " GROUP BY card_id ORDER BY s LIMIT 5", (since,))
    for r in rows:
        print(f"  {r['card_id']:<20}{r['s']:.1f}")
    due = db.execute("SELECT COUNT(*) FROM cards WHERE due <= ?", (now(),)).fetchone()[0]
    print(f"\ndue now: {due}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("daily", help="answer due cards")
    d.add_argument("-n", type=int, default=5)
    d.add_argument("-t", "--topic")
    d.add_argument("--speak", action="store_true", help="read questions aloud (macOS)")
    d.set_defaults(fn=cmd_daily)
    r = sub.add_parser("report", help="score summary")
    r.add_argument("-d", "--days", type=int, default=14)
    r.set_defaults(fn=cmd_report)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

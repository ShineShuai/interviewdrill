#!/usr/bin/env python3
"""problems: verified coding problems with hidden tests (programs read stdin, write stdout).

  problems.py gen [-t TOPIC] [-d 1-5] [-n 1]   LLM proposes a problem; it must pass verification
  problems.py review                            approve / reject verified candidates
  problems.py next [-t TOPIC]                   show the next due (or a new) problem
  problems.py solve ID [--reveal] -- CMD ...    run CMD on the tests, e.g. -- ./a.out
  problems.py status

Verification: reference == brute force on random small inputs, the statement examples match
the reference, the reference fits the time limit on large inputs, and the known-wrong
solutions fail the tests. Env: ANTHROPIC_API_KEY, DRILL_GEN_MODEL, DRILL_PROBLEMS, DRILL_SANDBOX.
"""
import argparse
import json
import os
import random
import re
import shutil
import sqlite3
import sys
import uuid
from collections import Counter
from pathlib import Path

import drill
import gen
import sandbox

PROBLEMS = Path(os.environ.get("DRILL_PROBLEMS", Path(__file__).parent / "problems"))
PENDING = PROBLEMS / "_pending"
N_SMALL, N_LARGE, LARGE = 40, 2, 50_000  # random small tests, large tests, large size
GROUPS = ("ex", "small", "large")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ex_state(
  key TEXT PRIMARY KEY, ease REAL DEFAULT 2.5, interval REAL DEFAULT 0,
  reps INTEGER DEFAULT 0, due TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS ex_attempts(
  id INTEGER PRIMARY KEY, key TEXT, ts TEXT, passed INTEGER, total INTEGER, detail TEXT,
  q REAL, uid TEXT, pushed INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS sync_state(key TEXT PRIMARY KEY, value TEXT);
"""

PROPOSE_SYSTEM = """You design programming-interview exercises with machine-checkable tests.
Programs read stdin and write stdout. The correct output must be uniquely determined.
Return:
- statement: Markdown with the task, input format, output format and constraints.
- examples: 2 pairs of exact input and output text.
- reference: efficient Python 3 solution (reads stdin, writes stdout).
- brute: obviously correct brute-force Python 3 solution (slow is fine).
- generator: Python 3 script; argv = seed size; prints one valid input. Sizes 1..8 are tiny
  cases (cover edge cases: minimum size, duplicates, negatives, ties); size 50000 is large.
- wrong: 2-3 plausible but incorrect Python solutions (off-by-one, missed edge case, too slow).
- approach: 2-4 sentences: the intended algorithm, its complexity and the key edge cases.
Standard library only. Use the seed for all randomness."""

STR = {"type": "string"}
PROPOSE_TOOL = {
    "name": "problem",
    "description": "Exercise: statement, reference, brute force, generator, wrong solutions.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": STR, "statement": STR, "difficulty": {"type": "integer"},
            "examples": {"type": "array", "items": {
                "type": "object", "properties": {"input": STR, "output": STR},
                "required": ["input", "output"]}},
            "reference": STR, "brute": STR, "generator": STR,
            "wrong": {"type": "array", "items": STR}, "approach": STR,
        },
        "required": ["title", "statement", "examples", "reference", "brute", "generator", "wrong"],
    },
}


def norm_out(s: str) -> str:
    return "\n".join(ln.rstrip() for ln in s.strip().splitlines())


def unique_slug(title: str) -> str:
    base = gen.slug(title)[:40] or "problem"
    name, n = base, 2
    while (PROBLEMS / name).exists() or (PENDING / name).exists():
        name, n = f"{base}-{n}", n + 1
    return name


def _spec_error(spec: object) -> str | None:
    if not isinstance(spec, dict):
        return "not an object"
    for k in ("title", "statement", "reference", "brute", "generator"):
        if not isinstance(spec.get(k), str) or not spec[k].strip():
            return f"missing {k}"
    ex = spec.get("examples")
    if not (isinstance(ex, list) and ex and all(
            isinstance(e, dict) and isinstance(e.get("input"), str)
            and isinstance(e.get("output"), str) for e in ex)):
        return "examples must be a non-empty list of {input, output} strings"
    return "statement too short" if len(spec["statement"]) < 80 else None


def verify(spec: dict, work: Path, small: int = N_SMALL, large: int = N_LARGE,
           big: int = LARGE) -> tuple[dict | None, str]:
    """Write the problem into `work` and run every check: (report, "") or (None, reason)."""
    if err := _spec_error(spec):
        return None, f"malformed spec: {err}"
    work.mkdir(parents=True, exist_ok=True)
    ref, brute, gen_py = (work / f"{n}.py" for n in ("reference", "brute", "generator"))
    for path, key in ((ref, "reference"), (brute, "brute"), (gen_py, "generator")):
        path.write_text(spec[key])
    wrong_src = [str(w) for w in (spec.get("wrong") or [])[:3]]
    wrong = [work / f"wrong{i}.py" for i in range(1, len(wrong_src) + 1)]
    for path, src in zip(wrong, wrong_src):
        path.write_text(src)
    cases: list[tuple[str, str, str]] = []  # (name, stdin, expected stdout)

    for i, ex in enumerate(spec["examples"][:3], 1):
        r = sandbox.run_py(ref, stdin=ex["input"], timeout=10)
        if not r.ok or norm_out(r.out) != norm_out(ex["output"]):
            return None, (f"reference disagrees with statement example {i}: expected "
                          f"{ex['output'][:80]!r}, got {r.out[:80]!r} {r.err[-150:]}")
        cases.append((f"ex{i}", ex["input"], r.out))

    inputs, outputs = set(), set()
    for seed in range(1, small + 1):
        g = sandbox.run_py(gen_py, str(seed), str(1 + seed % 8), timeout=10)
        if not g.ok or not g.out.strip() or len(g.out) > 100_000:
            return None, f"generator failed for seed {seed}: {g.err[-200:]}"
        a = sandbox.run_py(ref, stdin=g.out, timeout=10)
        b = sandbox.run_py(brute, stdin=g.out, timeout=10)
        if not (a.ok and b.ok):
            return None, f"a solution failed on input {g.out[:200]!r}: {(a.err or b.err)[-200:]}"
        if norm_out(a.out) != norm_out(b.out):
            return None, (f"reference and brute force disagree on input {g.out[:300]!r}: "
                          f"{a.out[:60]!r} vs {b.out[:60]!r}")
        inputs.add(g.out)
        outputs.add(norm_out(a.out))
        cases.append((f"small{seed:02d}", g.out, a.out))
    if len(inputs) < small // 4 or len(outputs) < 3:
        return None, f"degenerate tests: {len(inputs)} distinct inputs, {len(outputs)} outputs"

    ref_secs = 0.0
    for seed in range(1, large + 1):
        g = sandbox.run_py(gen_py, str(1000 + seed), str(big), timeout=30)
        a = sandbox.run_py(ref, stdin=g.out, timeout=30) if g.ok else g
        if not (g.ok and g.out.strip() and a.ok and a.out.strip()):
            return None, f"large case {seed} failed: {(a.err or 'timeout/empty')[-200:]}"
        ref_secs = max(ref_secs, a.secs)
        cases.append((f"large{seed}", g.out, a.out))
    limit = round(min(10.0, max(2.0, 4 * ref_secs)), 1)

    killed = 0
    for path in wrong:
        for _, inp, exp in cases:
            r = sandbox.run_py(path, stdin=inp, timeout=limit)
            if not r.ok or norm_out(r.out) != norm_out(exp):
                killed += 1
                break

    (work / "tests").mkdir(exist_ok=True)
    for name, inp, exp in cases:
        (work / "tests" / f"{name}.in").write_text(inp)
        (work / "tests" / f"{name}.out").write_text(exp)
    return {"small_agree": small, "large_ref_secs": round(ref_secs, 2), "time_limit": limit,
            "mutants": len(wrong), "mutants_killed": killed}, ""


def propose(topic, difficulty, avoid, prev=None, feedback=None) -> dict:
    prompt = f"Topic: {topic}\nDifficulty: {difficulty}/5 (1 = warm-up, 5 = hard)\n"
    if avoid:
        prompt += "Do not repeat these titles: " + "; ".join(avoid) + "\n"
    if feedback:
        prompt += (f"\nYour previous attempt failed verification: {feedback}\n"
                   f"Previous attempt:\n{json.dumps(prev)}\nReturn a corrected, complete problem.")
    return gen.call_tool(PROPOSE_SYSTEM, PROPOSE_TOOL, prompt, max_tokens=6000)


def make_problem(topic, difficulty, avoid, propose_fn=propose, repairs=2, **vkw):
    """Propose, verify, and feed failures back for repair. Returns (pending_dir | None, reason)."""
    prev, feedback, reason = None, None, ""
    for _ in range(repairs + 1):
        spec = propose_fn(topic, difficulty, avoid, prev, feedback)
        title = str(spec.get("title", "")).strip() if isinstance(spec, dict) else ""
        dup = next((t for t in avoid if title and gen.similar(title, t)), None)
        if dup:
            reason = f"title too similar to existing '{dup}'"
            prev, feedback = spec, reason
            continue
        work = PENDING / unique_slug(title)
        report, reason = verify(spec, work, **vkw)
        if report:
            meta = {"id": work.name, "title": title, "topic": topic,
                    "difficulty": max(1, min(5, int(spec.get("difficulty") or difficulty))),
                    "statement": spec["statement"], "examples": spec["examples"][:3],
                    "approach": str(spec.get("approach") or "").strip(),
                    "time_limit": report["time_limit"],
                    "provenance": {"model": gen.MODEL, "verified": report}}
            (work / "problem.json").write_text(json.dumps(meta, indent=2))
            return work, ""
        shutil.rmtree(work, ignore_errors=True)
        prev, feedback = spec, reason
    return None, reason


# --- progress (shared with bugs.py and sync.py) ---
def device_id(db) -> str:
    """Stable random id of this database; prefixes the uid of every attempt made here."""
    row = db.execute("SELECT value FROM sync_state WHERE key = 'device_id'").fetchone()
    if row:
        return row[0]
    dev = uuid.uuid4().hex[:8]
    db.execute("INSERT INTO sync_state VALUES('device_id', ?)", (dev,))
    return dev


def migrate(db) -> None:
    """Bring a database created before sync existed up to date (idempotent)."""
    cols = {r[1] for r in db.execute("PRAGMA table_info(ex_attempts)")}
    for name, ddl in (("q", "REAL"), ("uid", "TEXT"), ("pushed", "INTEGER DEFAULT 0")):
        if name not in cols:
            db.execute(f"ALTER TABLE ex_attempts ADD COLUMN {name} {ddl}")
    # progress recorded before the rename kata -> problem
    db.execute("UPDATE OR IGNORE ex_state SET key = 'problem:' || substr(key, 6) "
               "WHERE key LIKE 'kata:%'")
    db.execute("DELETE FROM ex_state WHERE key LIKE 'kata:%'")  # only if both ids existed
    db.execute("UPDATE ex_attempts SET key = 'problem:' || substr(key, 6) WHERE key LIKE 'kata:%'")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS ex_attempts_uid ON ex_attempts(uid)")
    # attempts from before sync were made on this device and have not been pushed
    db.execute("UPDATE ex_attempts SET uid = ? || ':' || id WHERE uid IS NULL", (device_id(db),))
    db.commit()


def connect() -> sqlite3.Connection:
    db = drill.connect()
    db.executescript(SCHEMA)
    migrate(db)
    return db


def record(db, key: str, q: float, passed: int, total: int, detail: str = "") -> float:
    """Store an attempt and reschedule `key` (SM-2 as in drill.py); returns interval in days."""
    row = db.execute("SELECT ease, interval, reps FROM ex_state WHERE key = ?", (key,)).fetchone()
    ease, interval, reps = drill.schedule(*(row or (2.5, 0.0, 0)), q)
    db.execute(
        "INSERT INTO ex_state(key, ease, interval, reps, due) VALUES(?,?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET ease = excluded.ease, interval = excluded.interval, "
        "reps = excluded.reps, due = excluded.due",
        (key, ease, interval, reps, drill.now(interval)),
    )
    db.execute("INSERT INTO ex_attempts(key, ts, passed, total, detail, q, uid, pushed) "
               "VALUES(?,?,?,?,?,?,?,0)",
               (key, drill.now(), passed, total, detail, q,
                f"{device_id(db)}:{uuid.uuid4().hex[:12]}"))
    db.commit()
    return interval


def autosync(db) -> None:
    """Best-effort sync after a practice session; a failure never breaks the session."""
    if not os.environ.get("DRILL_URL"):
        return
    try:
        import sync  # lazy: sync.py imports this module

        stats = sync.sync(db, sync.transport_from_env())
        print(f"synced: pushed {stats['pushed']}, pulled {stats['pulled']}")
    except Exception as exc:  # noqa: BLE001 - never fail a practice session over sync
        print(f"sync skipped: {exc}")


def pick(db, kind: str, ids: list[str], rng=random) -> str | None:
    """Most overdue exercise if any is due, else a random unseen one, else None."""
    due = dict(db.execute("SELECT key, due FROM ex_state WHERE key LIKE ?", (f"{kind}:%",)))
    now = drill.now()
    overdue = sorted((i for i in ids if f"{kind}:{i}" in due and due[f"{kind}:{i}"] <= now),
                     key=lambda i: due[f"{kind}:{i}"])
    unseen = [i for i in ids if f"{kind}:{i}" not in due]
    return overdue[0] if overdue else rng.choice(unseen) if unseen else None


# --- practice ---
def load_problems() -> dict[str, dict]:
    files = sorted(PROBLEMS.glob("*/problem.json"))
    return {m["id"]: m for m in (json.loads(f.read_text()) for f in files)}


def run_tests(kdir: Path, cmd: list[str]) -> list[dict]:
    meta = json.loads((kdir / "problem.json").read_text())
    group = lambda p: re.match(r"[a-z]+", p.stem).group()  # noqa: E731
    results = []
    for f in sorted((kdir / "tests").glob("*.in"), key=lambda p: (GROUPS.index(group(p)), p.stem)):
        inp, exp = f.read_text(), f.with_suffix(".out").read_text()
        r = sandbox.run(cmd, inp, timeout=meta["time_limit"], mem_mb=0)
        verdict = ("TLE" if r.timeout else "RE" if r.rc else
                   "WA" if norm_out(r.out) != norm_out(exp) else "OK")
        results.append({"name": f.stem, "group": group(f), "verdict": verdict, "input": inp,
                        "expected": exp, "got": r.out, "err": r.err, "secs": r.secs})
    return results


def summarize(results: list[dict], reveal: bool = False) -> tuple[int, int]:
    by = {g: [r for r in results if r["group"] == g] for g in GROUPS}
    print("   ".join(f"{g} {sum(r['verdict'] == 'OK' for r in rs)}/{len(rs)}"
                     for g, rs in by.items() if rs))
    bad = next((r for r in results if r["verdict"] != "OK"), None)
    if bad:
        print(f"first failure: {bad['name']} {bad['verdict']} ({bad['secs']:.2f}s)")
        if bad["verdict"] == "RE":
            print(f"  stderr: {bad['err'][-300:]}")
        if reveal or bad["group"] == "ex":
            print(f"  input:\n{bad['input'][:500]}\n  expected: {bad['expected'][:200]!r}"
                  f"\n  got: {bad['got'][:200]!r}")
        else:
            print("  hidden test; rerun with --reveal to see it")
    return sum(r["verdict"] == "OK" for r in results), len(results)


def show_problem(m: dict) -> None:
    print(f"\n{m['title']}  [{m['id']}, difficulty {m['difficulty']}, limit {m['time_limit']}s]\n")
    print(m["statement"])
    for i, ex in enumerate(m["examples"], 1):
        print(f"\nExample {i} input:\n{ex['input']}Output:\n{ex['output']}")
    print(f"\nsolve: problems.py solve {m['id']} -- <command that reads stdin>")


def card_for(meta: dict, kdir: Path) -> dict:
    """The problem as a web exercise: answered as an approach (or code), graded by an LLM."""
    examples = "\n\n".join(
        f"Example {i}\nInput:\n{e['input'].rstrip()}\nOutput:\n{e['output'].rstrip()}"
        for i, e in enumerate(meta["examples"], 1))
    question = (f"Problem: {meta['title']} - explain how you would solve it: algorithm, "
                "complexity, edge cases (code optional)."
                f"\n\n{meta['statement']}\n\n{examples}\n\n"
                f"Time limit: {meta['time_limit']}s per test.")
    ref = [f"- Intended approach: {meta['approach']}"] if meta.get("approach") else []
    ref.append("- Verified reference solution (Python; agrees with a brute force on random "
               "tests):\n" + (kdir / "reference.py").read_text()[:3000])
    for i, path in enumerate(sorted(kdir.glob("wrong*.py")), 1):
        ref.append(f"- Known wrong approach {i} (fails the tests):\n{path.read_text()[:1500]}")
    return {"id": f"problem:{meta['id']}", "topic": "problems", "question": question,
            "ref": "\n".join(ref)}


def tests_payload(kdir: Path) -> dict:
    """Tests of one problem for the browser runner (web/public/problems/<id>.json)."""
    meta = json.loads((kdir / "problem.json").read_text())
    group = lambda p: re.match(r"[a-z]+", p.stem).group()  # noqa: E731
    files = sorted((kdir / "tests").glob("*.in"), key=lambda p: (GROUPS.index(group(p)), p.stem))
    return {"id": meta["id"], "time_limit": meta["time_limit"],
            "tests": [{"name": f.stem, "input": f.read_text(),
                       "expected": f.with_suffix(".out").read_text()} for f in files]}


def review() -> Counter:
    done: Counter = Counter()
    for work in sorted(p.parent for p in PENDING.glob("*/problem.json")):
        meta = json.loads((work / "problem.json").read_text())
        v = meta["provenance"]["verified"]
        while True:
            show_problem({**meta, "id": work.name})
            print(f"\nverified: reference == brute force on {v['small_agree']} random tests; "
                  f"reference {v['large_ref_secs']}s on large input; wrong solutions killed "
                  f"{v['mutants_killed']}/{v['mutants']}\nfiles: {work}")
            key = input("[a]pprove [r]eject [s]kip [q]uit > ").strip().lower()[:1]
            if key == "a":
                shutil.move(work, PROBLEMS / (work.name if not (PROBLEMS / work.name).exists()
                                           else unique_slug(work.name)))
                done["approved"] += 1
                break
            if key == "r":
                shutil.rmtree(work)
                done["rejected"] += 1
                break
            if key == "s":
                done["skipped"] += 1
                break
            if key == "q":
                return done
    return done


# --- CLI ---
def cmd_gen(a):
    avoid = [m["title"] for m in load_problems().values()]
    avoid += [json.loads(p.read_text())["title"] for p in PENDING.glob("*/problem.json")]
    for _ in range(a.n):
        work, reason = make_problem(a.topic, a.difficulty, avoid)
        if work:
            avoid.append(json.loads((work / "problem.json").read_text())["title"])
            print(f"verified: {work}")
        else:
            print(f"rejected: {reason}")
    print("next: problems.py review")


def cmd_review(a):
    print(", ".join(f"{k}={v}" for k, v in sorted(review().items())) or "nothing pending")


def cmd_next(a):
    problems = {k: m for k, m in load_problems().items() if not a.topic or m["topic"] == a.topic}
    kid = pick(connect(), "problem", sorted(problems))
    if kid:
        show_problem(problems[kid])
    else:
        print("nothing due" if problems else "no problems; run: problems.py gen")


def cmd_solve(a):
    cmd = a.cmd
    kdir = PROBLEMS / a.id
    if not cmd or not (kdir / "problem.json").exists():
        raise SystemExit("usage: problems.py solve ID [--reveal] -- CMD ... (ID must be approved)")
    passed, total = summarize(run_tests(kdir, cmd), a.reveal)
    q = 5 if passed == total else 3 if passed >= 0.8 * total else 1
    db = connect()
    days = record(db, f"problem:{a.id}", q, passed, total)
    print(f"{passed}/{total} passed; next review in {days:.0f}d")
    autosync(db)


def cmd_status(a):
    db = connect()
    row = db.execute("SELECT COUNT(*), COALESCE(SUM(passed = total), 0) FROM ex_attempts "
                     "WHERE key LIKE 'problem:%'").fetchone()
    pending = len(list(PENDING.glob("*/problem.json")))
    print(f"problems: {len(load_problems())} approved, {pending} pending; "
          f"attempts {row[0]} ({row[1]} fully passed)")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("gen", help="generate and verify problems")
    s.add_argument("-t", "--topic", default="arrays and strings")
    s.add_argument("-d", "--difficulty", type=int, default=2)
    s.add_argument("-n", type=int, default=1)
    s.set_defaults(fn=cmd_gen)
    sub.add_parser("review", help="approve verified candidates").set_defaults(fn=cmd_review)
    s = sub.add_parser("next", help="show the next problem")
    s.add_argument("-t", "--topic")
    s.set_defaults(fn=cmd_next)
    s = sub.add_parser("solve", help="run your program on the tests")
    s.add_argument("id")
    s.add_argument("--reveal", action="store_true")
    s.set_defaults(fn=cmd_solve)
    sub.add_parser("status", help="counts").set_defaults(fn=cmd_status)
    argv = sys.argv[1:]
    cmd: list[str] = []
    if "--" in argv:  # everything after -- is the program to test, untouched by argparse
        i = argv.index("--")
        argv, cmd = argv[:i], argv[i + 1:]
    args = p.parse_args(argv)
    args.cmd = cmd
    args.fn(args)


if __name__ == "__main__":
    main()

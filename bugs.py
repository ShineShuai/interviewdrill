#!/usr/bin/env python3
"""bugs: C++ debugging exercises whose bug is confirmed by a sanitizer.

  bugs.py gen [-c CATEGORY] [-d 1-5] [-n 1]   LLM writes buggy + fixed program; must verify
  bugs.py review                               approve / reject verified candidates
  bugs.py next [-c CATEGORY]                   read the code, write a diagnosis, get graded
  bugs.py status

Verification: the buggy program triggers the expected sanitizer report on every run, the
fixed program runs clean, and the fix is a small diff. Needs g++ (CXX=clang++ also works)
with ASan/UBSan/TSan. Env: ANTHROPIC_API_KEY, DRILL_GEN_MODEL, DRILL_BUGS, CXX.
"""
import argparse
import difflib
import json
import os
import random
import re
import shutil
from collections import Counter
from pathlib import Path

import drill
import gen
import kata
import sandbox

BUGS = Path(os.environ.get("DRILL_BUGS", Path(__file__).parent / "bugs"))
PENDING = BUGS / "_pending"
CXX = os.environ.get("CXX", "g++")
RUNS = 3  # the report must reproduce on every run
MAX_DIFF = 12  # changed lines allowed in the fix

# category -> (sanitizer, text the first report must contain)
CATEGORIES = {
    "heap-use-after-free": ("address", "heap-use-after-free"),
    "heap-buffer-overflow": ("address", "heap-buffer-overflow"),
    "stack-buffer-overflow": ("address", "stack-buffer-overflow"),
    "double-free": ("address", "double-free"),
    "memory-leak": ("address", "detected memory leaks"),
    "signed-overflow": ("undefined", "signed integer overflow"),
    "data-race": ("thread", "data race"),
    "lock-order-inversion": ("thread", "lock-order-inversion"),
}

REPORT = re.compile(r"AddressSanitizer: (\S+(?: \S+)?)|LeakSanitizer: (detected memory leaks)"
                    r"|ThreadSanitizer: ([\w -]+?) \(|runtime error: ([\w -]+)")
SAN_MARK = re.compile(r"Sanitizer|runtime error")
DENY = re.compile(
    r"\b(system|popen|fork|vfork|execl?p?e?|execv?p?e?|fopen|freopen|socket|dlopen|ptrace|asm)"
    r"\s*\(|#\s*include\s*<(fstream|filesystem|unistd\.h|sys/|netinet/|arpa/|dlfcn\.h|signal\.h"
    r"|csignal)")

STR = {"type": "string"}
PROPOSE_SYSTEM = """You write C++20 debugging exercises for interview practice. Each has exactly
one deliberate bug that a sanitizer reports.
- buggy: complete, self-contained, deterministic program (standard library only, 20-60 lines)
  that prints something and returns 0 when correct. The bug is realistic and subtle, not a
  typo, and it triggers on every run.
- fixed: the same program with the minimal correction (change as few lines as possible).
- category: the requested sanitizer finding.
- explanation: root cause and fix in two sentences.
- Data races: use std::thread and join; both threads touch the shared variable on every run.
- Lock-order inversion: two mutexes locked in opposite orders by two threads that run one
  after the other (join the first before starting the second), so it never really deadlocks.
- No files, network, processes, signals or system calls."""
PROPOSE_TOOL = {
    "name": "bug",
    "description": "A buggy C++ program, its minimal fix and the root cause.",
    "input_schema": {
        "type": "object",
        "properties": {"title": STR, "category": {"type": "string", "enum": list(CATEGORIES)},
                       "difficulty": {"type": "integer"}, "buggy": STR, "fixed": STR,
                       "explanation": STR},
        "required": ["title", "category", "buggy", "fixed", "explanation"],
    },
}


def first_report(stderr: str) -> str:
    m = REPORT.search(stderr)
    return next((g for g in m.groups() if g), "") if m else ""


def compile_cmd(sanitizer: str, src: Path, out: Path) -> list[str]:
    flags = [f"-fsanitize={sanitizer}"]
    if sanitizer == "undefined":
        flags.append("-fno-sanitize-recover=undefined")  # stop at the first finding
    return [CXX, "-std=c++20", "-g", "-O0", "-fno-omit-frame-pointer", "-pthread",
            *flags, "-o", str(out), str(src)]


def _spec_error(spec: object) -> str | None:
    if not isinstance(spec, dict):
        return "not an object"
    for k in ("title", "category", "buggy", "fixed", "explanation"):
        if not isinstance(spec.get(k), str) or not spec[k].strip():
            return f"missing {k}"
    if spec["category"] not in CATEGORIES:
        return f"unknown category {spec['category']!r}"
    return None


def verify(spec: dict, work: Path, runs: int = RUNS) -> tuple[dict | None, str]:
    """Compile, run under the sanitizer, check the fix: (report, "") or (None, reason)."""
    if err := _spec_error(spec):
        return None, f"malformed spec: {err}"
    cat = spec["category"]
    sanitizer, signature = CATEGORIES[cat]
    for name in ("buggy", "fixed"):
        if DENY.search(spec[name]):
            return None, f"{name} uses a forbidden call or header"
    work.mkdir(parents=True, exist_ok=True)
    for name in ("buggy", "fixed"):
        src = work / f"{name}.cpp"
        src.write_text(spec[name])
        c = sandbox.run(compile_cmd(sanitizer, src, work / name), timeout=120, mem_mb=0, cwd=work)
        if not c.ok:
            return None, f"{name} does not compile: {c.err[:400]}"
    env = {"ASAN_OPTIONS": f"detect_leaks={int(cat == 'memory-leak')}"}

    def run(name: str):
        return sandbox.run([str(work / name)], timeout=10, mem_mb=0, cwd=work, env=env)

    reports = []
    for _ in range(runs):
        r = run("buggy")
        kind = first_report(r.err)
        if r.rc == 0 or signature not in kind:
            return None, (f"buggy: expected a '{signature}' report, got '{kind or 'none'}' "
                          f"(rc {r.rc}): {r.err[:200]}")
        reports.append(r.err)
    for _ in range(runs):
        r = run("fixed")
        if not r.ok or SAN_MARK.search(r.err):
            return None, f"fixed is not clean (rc {r.rc}): {r.err[:300]}"
    diff = list(difflib.unified_diff(spec["buggy"].splitlines(), spec["fixed"].splitlines(),
                                     "buggy.cpp", "fixed.cpp", lineterm="", n=1))
    changed = sum(ln.startswith(("+", "-")) and not ln.startswith(("+++", "---")) for ln in diff)
    if not 1 <= changed <= MAX_DIFF:
        return None, f"the fix must be a small diff (changed lines: {changed})"
    excerpt = reports[0].replace(str(work.resolve()), ".")
    excerpt = re.sub(r"==\d+==|\(pid=\d+\)", "", excerpt)
    return {"category": cat, "sanitizer": sanitizer, "runs": runs, "changed_lines": changed,
            "excerpt": "\n".join(excerpt.strip().splitlines()[:14]), "diff": "\n".join(diff)}, ""


def propose(category, difficulty, avoid, prev=None, feedback=None) -> dict:
    prompt = f"Category: {category}\nDifficulty: {difficulty}/5\n"
    if avoid:
        prompt += "Do not repeat these titles: " + "; ".join(avoid) + "\n"
    if feedback:
        prompt += (f"\nYour previous attempt failed verification: {feedback}\n"
                   f"Previous attempt:\n{json.dumps(prev)}\nReturn a corrected, full exercise.")
    return gen.call_tool(PROPOSE_SYSTEM, PROPOSE_TOOL, prompt, max_tokens=3000)


def unique_slug(title: str) -> str:
    base = gen.slug(title)[:40] or "bug"
    name, n = base, 2
    while (BUGS / name).exists() or (PENDING / name).exists():
        name, n = f"{base}-{n}", n + 1
    return name


def make_bug(category, difficulty, avoid, propose_fn=propose, repairs=2, **vkw):
    """Propose, verify, feed failures back for repair. Returns (pending_dir | None, reason)."""
    prev, feedback, reason = None, None, ""
    for _ in range(repairs + 1):
        spec = propose_fn(category, difficulty, avoid, prev, feedback)
        title = str(spec.get("title", "")).strip() if isinstance(spec, dict) else ""
        dup = next((t for t in avoid if title and gen.similar(title, t)), None)
        if dup:
            reason = f"title too similar to existing '{dup}'"
            prev, feedback = spec, reason
            continue
        if isinstance(spec, dict):
            spec = {**spec, "category": category}  # the requested category wins
        work = PENDING / unique_slug(title)
        report, reason = verify(spec, work, **vkw)
        if report:
            meta = {"id": work.name, "title": title, "category": category,
                    "difficulty": max(1, min(5, int(spec.get("difficulty") or difficulty))),
                    "explanation": spec["explanation"],
                    "provenance": {"model": gen.MODEL, "verified": report}}
            (work / "bug.json").write_text(json.dumps(meta, indent=2))
            return work, ""
        shutil.rmtree(work, ignore_errors=True)
        prev, feedback = spec, reason
    return None, reason


def load_bugs() -> dict[str, dict]:
    files = sorted(BUGS.glob("*/bug.json"))
    return {m["id"]: m for m in (json.loads(f.read_text()) for f in files)}


def numbered(code: str) -> str:
    return "\n".join(f"{i:3} | {ln}" for i, ln in enumerate(code.splitlines(), 1))


def ground_truth(meta: dict) -> str:
    v = meta["provenance"]["verified"]
    return (f"- Bug class: {meta['category']}\n- Root cause and fix: {meta['explanation']}\n"
            f"- Sanitizer report: {v['excerpt'].splitlines()[0] if v['excerpt'] else ''}\n"
            f"- Verified fix (diff):\n{v['diff']}")


def review() -> Counter:
    done: Counter = Counter()
    for work in sorted(p.parent for p in PENDING.glob("*/bug.json")):
        meta = json.loads((work / "bug.json").read_text())
        v = meta["provenance"]["verified"]
        while True:
            print(f"\n{meta['title']} [{meta['category']}, level {meta['difficulty']}] {work}")
            print(numbered((work / "buggy.cpp").read_text()))
            print(f"\nsanitizer (all {v['runs']} runs):\n{v['excerpt']}\n\nfix:\n{v['diff']}")
            print(f"\nexplanation: {meta['explanation']}")
            key = input("[a]pprove [r]eject [s]kip [q]uit > ").strip().lower()[:1]
            if key == "a":
                dest = BUGS / (work.name if not (BUGS / work.name).exists()
                               else unique_slug(work.name))
                shutil.move(work, dest)
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


def practice(db, category: str | None = None, rng=random) -> bool:
    bugs = {k: m for k, m in load_bugs().items() if not category or m["category"] == category}
    bid = kata.pick(db, "bug", sorted(bugs), rng)
    if not bid:
        print("nothing due" if bugs else "no bugs to practice; run: bugs.py gen")
        return False
    meta = bugs[bid]
    code = (BUGS / bid / "buggy.cpp").read_text()
    print(f"\nThis C++ program has one bug.\n\n{numbered(code)}\n")
    print("Name the bug class, the root cause and the fix (finish with an empty line):")
    answer = drill.read_answer()
    question = f"Find the single bug in this C++ program, explain it, and fix it.\n\n{code}"
    card = {"question": question, "ref": ground_truth(meta)}
    g = drill.judge(card, answer)
    q = drill.score(g)
    days = kata.record(db, f"bug:{bid}", q, round(q), 5, json.dumps(g))
    drill.show(g, q, days)
    print(f"\nground truth:\n{ground_truth(meta)}")
    return True


# --- CLI ---
def cmd_gen(a):
    avoid = [m["title"] for m in load_bugs().values()]
    avoid += [json.loads(p.read_text())["title"] for p in PENDING.glob("*/bug.json")]
    for _ in range(a.n):
        work, reason = make_bug(a.category or random.choice(list(CATEGORIES)), a.difficulty, avoid)
        if work:
            avoid.append(json.loads((work / "bug.json").read_text())["title"])
            print(f"verified: {work}")
        else:
            print(f"rejected: {reason}")
    print("next: bugs.py review")


def cmd_review(a):
    print(", ".join(f"{k}={v}" for k, v in sorted(review().items())) or "nothing pending")


def cmd_next(a):
    practice(kata.connect(), a.category)


def cmd_status(a):
    by = Counter(m["category"] for m in load_bugs().values())
    pending = len(list(PENDING.glob("*/bug.json")))
    print(f"bugs: {sum(by.values())} approved, {pending} pending; {dict(by)}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("gen", help="generate and verify bugs")
    s.add_argument("-c", "--category", choices=list(CATEGORIES))
    s.add_argument("-d", "--difficulty", type=int, default=2)
    s.add_argument("-n", type=int, default=1)
    s.set_defaults(fn=cmd_gen)
    sub.add_parser("review", help="approve verified candidates").set_defaults(fn=cmd_review)
    s = sub.add_parser("next", help="practice the next bug")
    s.add_argument("-c", "--category", choices=list(CATEGORIES))
    s.set_defaults(fn=cmd_next)
    sub.add_parser("status", help="counts").set_defaults(fn=cmd_status)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""bugs: debugging exercises (C++ and Go) whose bug is confirmed by a tool.

  bugs.py gen [-c CATEGORY] [-d 1-5] [-n 1]   LLM writes buggy + fixed program; must verify
  bugs.py review                               approve / reject verified candidates
  bugs.py next [-c CATEGORY]                   read the code, write a diagnosis, get graded
  bugs.py status

Verification: the buggy program triggers the expected report (sanitizer, Go race detector or
Go runtime) on every run, the fixed program runs clean, and the fix is a small diff.
C++ needs g++ or clang++ with ASan/UBSan/TSan (CXX=...). Go needs `go` (GO=...) and, for
go-data-race, cgo (a C compiler). Env: ANTHROPIC_API_KEY, DRILL_GEN_MODEL, DRILL_BUGS.
"""
import argparse
import difflib
import json
import os
import random
import re
import shutil
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import drill
import gen
import kata
import sandbox

BUGS = Path(os.environ.get("DRILL_BUGS", Path(__file__).parent / "bugs"))
PENDING = BUGS / "_pending"
CXX = os.environ.get("CXX", "g++")
GO = os.environ.get("GO", "go")
RUNS = 3  # the report must reproduce on every run
MAX_DIFF = 12  # changed lines allowed in the fix

REPORT = re.compile(r"AddressSanitizer: (\S+(?: \S+)?)|LeakSanitizer: (detected memory leaks)"
                    r"|ThreadSanitizer: ([\w -]+?) \(|runtime error: ([\w -]+)")
SAN_MARK = re.compile(r"Sanitizer|runtime error")
DENY = re.compile(
    r"\b(system|popen|fork|vfork|execl?p?e?|execv?p?e?|fopen|freopen|socket|dlopen|ptrace|asm)"
    r"\s*\(|#\s*include\s*<(fstream|filesystem|unistd\.h|sys/|netinet/|arpa/|dlfcn\.h|signal\.h"
    r"|csignal)")
GO_REPORT = re.compile(r"WARNING: (DATA RACE)|fatal error: ([^\n]+)|panic: ([^\n]+)")
GO_MARK = re.compile(r"DATA RACE|fatal error|panic:|goroutine \d+ \[")
DENY_GO = re.compile(
    r'"(os/exec|syscall|unsafe|plugin|C|net|net/[\w/]+|io/ioutil|debug/[\w/]+|runtime/debug)"'
    r"|\bos\.(Open|OpenFile|Create|CreateTemp|Remove|RemoveAll|Rename|Mkdir|MkdirAll|WriteFile"
    r"|ReadFile|Chdir|StartProcess|Setenv|Symlink|Link)\b|//go:(linkname|embed|generate)|#cgo")


@dataclass(frozen=True)
class Lang:
    name: str
    ext: str
    exe: str
    deny: re.Pattern  # forbidden calls and imports (LLM-written code runs on this machine)
    mark: re.Pattern  # any sign of a finding; must be absent from the fixed program's stderr
    report: re.Pattern  # extracts the first finding


@dataclass(frozen=True)
class Category:
    lang: str  # key of LANGS
    tool: str  # sanitizer (C++), "race" (Go), "" when the runtime itself reports it
    signature: str  # text the first report must contain


LANGS = {
    "cpp": Lang("C++", "cpp", CXX, DENY, SAN_MARK, REPORT),
    "go": Lang("Go", "go", GO, DENY_GO, GO_MARK, GO_REPORT),
}
CATEGORIES = {
    "heap-use-after-free": Category("cpp", "address", "heap-use-after-free"),
    "heap-buffer-overflow": Category("cpp", "address", "heap-buffer-overflow"),
    "stack-buffer-overflow": Category("cpp", "address", "stack-buffer-overflow"),
    "double-free": Category("cpp", "address", "double-free"),
    "memory-leak": Category("cpp", "address", "detected memory leaks"),
    "signed-overflow": Category("cpp", "undefined", "signed integer overflow"),
    "data-race": Category("cpp", "thread", "data race"),
    "lock-order-inversion": Category("cpp", "thread", "lock-order-inversion"),
    "go-data-race": Category("go", "race", "DATA RACE"),
    "go-deadlock": Category("go", "", "all goroutines are asleep - deadlock"),
    "go-nil-map-write": Category("go", "", "assignment to entry in nil map"),
    "go-closed-channel": Category("go", "", "closed channel"),
}

STR = {"type": "string"}
PROPOSE_SYSTEM = {
    "cpp": """You write C++20 debugging exercises for interview practice. Each has exactly
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
- No files, network, processes, signals or system calls.""",
    "go": """You write Go 1.21 debugging exercises for interview practice. Each has exactly one
deliberate bug that the Go race detector or runtime reports.
- buggy: complete `package main` program (standard library only, 20-60 lines), deterministic,
  prints something and exits 0 when correct. The bug is realistic and subtle, not a typo, and
  it triggers on every run.
- fixed: the same program with the minimal correction (change as few lines as possible).
- category: the requested finding.
- explanation: root cause and fix in two sentences.
- go-data-race: two or more goroutines touch a shared variable without synchronization; use
  sync.WaitGroup so the program always finishes. `go build -race` must report it.
- go-deadlock: main blocks forever and every goroutine is asleep (for example an unbuffered
  channel send with no receiver). Never hang with time.Sleep or select{}.
- go-nil-map-write: a write to a nil map. go-closed-channel: a send on a closed channel or a
  second close of a channel.
- No files, network, processes, cgo, unsafe or os/exec.""",
}
PROPOSE_TOOL = {
    "name": "bug",
    "description": "A buggy program, its minimal fix and the root cause.",
    "input_schema": {
        "type": "object",
        "properties": {"title": STR, "category": {"type": "string", "enum": list(CATEGORIES)},
                       "difficulty": {"type": "integer"}, "buggy": STR, "fixed": STR,
                       "explanation": STR},
        "required": ["title", "category", "buggy", "fixed", "explanation"],
    },
}


def first_report(stderr: str, lang: str = "cpp") -> str:
    m = LANGS[lang].report.search(stderr)
    return next((g for g in m.groups() if g), "") if m else ""


def compile_cmd(sanitizer: str, src: Path, out: Path) -> list[str]:
    flags = [f"-fsanitize={sanitizer}"]
    if sanitizer == "undefined":
        flags.append("-fno-sanitize-recover=undefined")  # stop at the first finding
    return [CXX, "-std=c++20", "-g", "-O0", "-fno-omit-frame-pointer", "-pthread",
            *flags, "-o", str(out), str(src)]


def build_cmd(cat: Category, src: Path, out: Path) -> list[str]:
    if cat.lang == "go":
        return [GO, "build", *(["-race"] if cat.tool == "race" else []), "-o", str(out), str(src)]
    return compile_cmd(cat.tool, src, out)


def build_env(cat: Category) -> dict[str, str]:
    """Go builds need a cache and compiler locations; never a proxy or toolchain download."""
    if cat.lang != "go":
        return {}
    keep = ("HOME", "GOCACHE", "GOPATH", "GOROOT", "TMPDIR", "XDG_CACHE_HOME", "CC",
            "SDKROOT", "DEVELOPER_DIR")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    # -race needs cgo; everything else builds without a C toolchain
    return {**env, "GOTOOLCHAIN": "local", "GOPROXY": "off",
            "CGO_ENABLED": "1" if cat.tool == "race" else "0"}


def available() -> list[str]:
    """Categories whose toolchain is installed."""
    return [c for c, cat in CATEGORIES.items() if shutil.which(LANGS[cat.lang].exe)]


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
    """Build, run under the tool, check the fix: (report, "") or (None, reason)."""
    if err := _spec_error(spec):
        return None, f"malformed spec: {err}"
    cat_name = spec["category"]
    cat = CATEGORIES[cat_name]
    lang = LANGS[cat.lang]
    if not shutil.which(lang.exe):
        return None, f"{lang.exe} not found on PATH"
    for name in ("buggy", "fixed"):
        if lang.deny.search(spec[name]):
            return None, f"{name} uses a forbidden call or import"
    work.mkdir(parents=True, exist_ok=True)
    if cat.lang == "go":
        (work / "go.mod").write_text("module bug\n\ngo 1.21\n")
    for name in ("buggy", "fixed"):
        src = work / f"{name}.{lang.ext}"
        src.write_text(spec[name])
        c = sandbox.run(build_cmd(cat, src, work / name), timeout=300, mem_mb=0, cwd=work,
                        env=build_env(cat))
        if not c.ok:
            return None, f"{name} does not compile: {c.err[:400]}"
    env = {"ASAN_OPTIONS": f"detect_leaks={int(cat_name == 'memory-leak')}"}
    env = env if cat.lang == "cpp" else {}

    def run(name: str):
        return sandbox.run([str(work / name)], timeout=10, mem_mb=0, cwd=work, env=env)

    reports = []
    for _ in range(runs):
        r = run("buggy")
        kind = first_report(r.err, cat.lang)
        if r.rc == 0 or cat.signature not in kind:
            return None, (f"buggy: expected a '{cat.signature}' report, got '{kind or 'none'}' "
                          f"(rc {r.rc}): {r.err[:200]}")
        reports.append(r.err)
    for _ in range(runs):
        r = run("fixed")
        if not r.ok or lang.mark.search(r.err):
            return None, f"fixed is not clean (rc {r.rc}): {r.err[:300]}"
    diff = list(difflib.unified_diff(
        spec["buggy"].splitlines(), spec["fixed"].splitlines(),
        f"buggy.{lang.ext}", f"fixed.{lang.ext}", lineterm="", n=1))
    changed = sum(ln.startswith(("+", "-")) and not ln.startswith(("+++", "---")) for ln in diff)
    if not 1 <= changed <= MAX_DIFF:
        return None, f"the fix must be a small diff (changed lines: {changed})"
    excerpt = reports[0].replace(str(work.resolve()), ".")
    excerpt = re.sub(r"==\d+==|\(pid=\d+\)", "", excerpt)
    excerpt = re.sub(r"0x[0-9a-f]{6,}", "0xADDR", excerpt)
    return {"category": cat_name, "tool": cat.tool, "runs": runs, "changed_lines": changed,
            "excerpt": "\n".join(excerpt.strip().splitlines()[:14]), "diff": "\n".join(diff)}, ""


def propose(category, difficulty, avoid, prev=None, feedback=None) -> dict:
    prompt = f"Category: {category}\nDifficulty: {difficulty}/5\n"
    if avoid:
        prompt += "Do not repeat these titles: " + "; ".join(avoid) + "\n"
    if feedback:
        prompt += (f"\nYour previous attempt failed verification: {feedback}\n"
                   f"Previous attempt:\n{json.dumps(prev)}\nReturn a corrected, full exercise.")
    system = PROPOSE_SYSTEM[CATEGORIES[category].lang]
    return gen.call_tool(system, PROPOSE_TOOL, prompt, max_tokens=3000)


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
                    "lang": CATEGORIES[category].lang,
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


def lang_of(meta: dict) -> Lang:
    return LANGS[meta.get("lang", "cpp")]  # exercises approved before Go support are C++


def numbered(code: str) -> str:
    return "\n".join(f"{i:3} | {ln}" for i, ln in enumerate(code.splitlines(), 1))


def report_line(excerpt: str) -> str:
    """First line of a tool report that is not just a separator."""
    return next((ln for ln in excerpt.splitlines() if ln.strip("= ")), "")


def ground_truth(meta: dict) -> str:
    v = meta["provenance"]["verified"]
    return (f"- Bug class: {meta['category']}\n- Root cause and fix: {meta['explanation']}\n"
            f"- Tool report: {report_line(v['excerpt'])}\n"
            f"- Verified fix (diff):\n{v['diff']}")


def card_for(meta: dict, bdir: Path) -> dict:
    """The bug as an exercise (CLI and web): question = numbered code, ref = verified truth."""
    lang = lang_of(meta)
    code = (bdir / f"buggy.{lang.ext}").read_text()
    question = (f"Find the single bug in this {lang.name} program, explain its root cause and "
                f"how to fix it.\n\n{numbered(code)}")
    return {"id": f"bug:{meta['id']}", "topic": "bugs", "question": question,
            "ref": ground_truth(meta)}


def review() -> Counter:
    done: Counter = Counter()
    for work in sorted(p.parent for p in PENDING.glob("*/bug.json")):
        meta = json.loads((work / "bug.json").read_text())
        v = meta["provenance"]["verified"]
        while True:
            print(f"\n{meta['title']} [{meta['category']}, level {meta['difficulty']}] {work}")
            print(numbered((work / f"buggy.{lang_of(meta).ext}").read_text()))
            print(f"\nreport (all {v['runs']} runs):\n{v['excerpt']}\n\nfix:\n{v['diff']}")
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
    card = card_for(meta, BUGS / bid)
    print(f"\n{card['question']}\n")
    print("Name the bug class, the root cause and the fix (finish with an empty line):")
    g = drill.judge(card, drill.read_answer())
    q = drill.score(g)
    days = kata.record(db, f"bug:{bid}", q, round(q), 5, json.dumps(g))
    drill.show(g, q, days)
    print(f"\nground truth:\n{ground_truth(meta)}")
    return True


# --- CLI ---
def cmd_gen(a):
    avoid = [m["title"] for m in load_bugs().values()]
    avoid += [json.loads(p.read_text())["title"] for p in PENDING.glob("*/bug.json")]
    pool = available()
    if not (a.category or pool):
        raise SystemExit("no toolchain found: install g++/clang++ or go")
    for _ in range(a.n):
        work, reason = make_bug(a.category or random.choice(pool), a.difficulty, avoid)
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

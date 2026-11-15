import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import drill
import export_cards
import kata

REF = """import sys
a = list(map(int, sys.stdin.read().split()))[1:]
best = cur = a[0]
for x in a[1:]:
    cur = max(x, cur + x)
    best = max(best, cur)
print(best)
"""
BRUTE = """import sys
a = list(map(int, sys.stdin.read().split()))[1:]
print(max(sum(a[i:j]) for i in range(len(a)) for j in range(i + 1, len(a) + 1)))
"""
GEN = """import random, sys
seed, n = int(sys.argv[1]), int(sys.argv[2])
rng = random.Random(seed)
hi = -1 if seed % 5 == 0 else 10
print(n)
print(*[rng.randint(-10, hi) for _ in range(n)])
"""
WRONG_EMPTY = """import sys
a = list(map(int, sys.stdin.read().split()))[1:]
best = cur = 0
for x in a:
    cur = max(0, cur + x)
    best = max(best, cur)
print(best)
"""
WRONG_SLOW = """import sys
a = list(map(int, sys.stdin.read().split()))[1:]
best = a[0]
for i in range(len(a)):
    s = 0
    for j in range(i, len(a)):
        s += a[j]
        best = max(best, s)
print(best)
"""
SPEC = {
    "title": "Maximum subarray sum",
    "statement": "Given n integers, print the largest sum of a non-empty contiguous subarray.\n"
                 "Input: n, then n integers. Output: one integer. 1 <= n <= 50000.",
    "difficulty": 2,
    "examples": [{"input": "5\n1 -2 3 4 -1\n", "output": "7\n"},
                 {"input": "3\n-3 -1 -2\n", "output": "-1\n"}],
    "reference": REF, "brute": BRUTE, "generator": GEN, "wrong": [WRONG_EMPTY, WRONG_SLOW],
    "approach": "Kadane's algorithm: O(n) time, O(1) space; handle all-negative input.",
}
FAST = dict(small=12, large=1, big=20000)  # keep tests quick; O(n^2) at 20k still times out


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.patches = [
            mock.patch.object(kata, "KATAS", root / "katas"),
            mock.patch.object(kata, "PENDING", root / "katas" / "_pending"),
            mock.patch.object(drill, "HOME", root / "home"),
            mock.patch.object(drill, "CARDS", root / "cards"),
        ]
        (root / "cards").mkdir()
        for p in cls.patches:
            p.start()

    @classmethod
    def tearDownClass(cls):
        for p in cls.patches:
            p.stop()
        cls.tmp.cleanup()


class VerifyTest(Base):
    def test_good_spec_passes_and_writes_tests(self):
        work = kata.PENDING / "good"
        report, reason = kata.verify(SPEC, work, **FAST)
        self.assertEqual(reason, "")
        self.assertEqual((report["mutants"], report["mutants_killed"]), (2, 2))
        names = sorted(p.stem for p in (work / "tests").glob("*.in"))
        self.assertEqual(len(names), 2 + 12 + 1)
        self.assertEqual((work / "tests" / "ex1.out").read_text().strip(), "7")

    def check_rejects(self, spec, needle):
        report, reason = kata.verify(spec, kata.PENDING / "bad", **FAST)
        self.assertIsNone(report)
        self.assertIn(needle, reason)

    def test_rejects_malformed(self):
        self.check_rejects({**SPEC, "statement": "short"}, "statement too short")
        self.check_rejects({**SPEC, "examples": []}, "examples")
        self.check_rejects({k: v for k, v in SPEC.items() if k != "brute"}, "missing brute")

    def test_rejects_reference_contradicting_example(self):
        bad = {**SPEC, "examples": [{"input": "3\n1 2 3\n", "output": "7\n"}]}
        self.check_rejects(bad, "example 1")

    def test_rejects_when_brute_force_disagrees(self):
        self.check_rejects({**SPEC, "brute": "print(0)"}, "disagree")

    def test_rejects_degenerate_generator(self):
        self.check_rejects({**SPEC, "generator": "print(1)\nprint(5)"}, "degenerate")

    def test_rejects_crashing_generator(self):
        self.check_rejects({**SPEC, "generator": "raise SystemExit(3)"}, "generator failed")


class RepairTest(Base):
    def test_failure_is_fed_back_then_succeeds(self):
        calls = []

        def propose(topic, difficulty, avoid, prev, feedback):
            calls.append((prev, feedback))
            return {**SPEC, "brute": "print(0)"} if len(calls) == 1 else SPEC

        work, reason = kata.make_kata("arrays", 2, [], propose, repairs=1, **FAST)
        self.assertEqual(reason, "")
        self.assertEqual(calls[0], (None, None))
        self.assertIn("disagree", calls[1][1])
        self.assertEqual(calls[1][0]["brute"], "print(0)")
        meta = json.loads((work / "kata.json").read_text())
        self.assertEqual((meta["id"], meta["topic"]), (work.name, "arrays"))
        shutil.rmtree(work)

    def test_gives_up_and_cleans_up(self):
        bad = {**SPEC, "brute": "print(0)"}
        work, reason = kata.make_kata("arrays", 2, [], lambda *a: bad, repairs=1, **FAST)
        self.assertIsNone(work)
        self.assertIn("disagree", reason)
        self.assertEqual(list(kata.PENDING.glob("*")), [])

    def test_similar_title_is_rejected_without_verification(self):
        seen = []

        def propose(*args):
            seen.append(args[-1])
            return SPEC

        work, reason = kata.make_kata("a", 2, ["Maximum subarray sum"], propose, repairs=1, **FAST)
        self.assertIsNone(work)
        self.assertIn("too similar", reason)
        self.assertEqual(len(seen), 2)


class PracticeTest(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        work, reason = kata.make_kata("arrays", 2, [], lambda *a: SPEC, **FAST)
        assert work, reason
        quiet = contextlib.redirect_stdout(io.StringIO())
        with mock.patch("builtins.input", side_effect=["a"]), quiet:
            assert dict(kata.review()) == {"approved": 1}
        cls.kid = work.name
        cls.sols = Path(cls.tmp.name, "sols")
        cls.sols.mkdir()
        for name, src in {"good": REF, "empty": WRONG_EMPTY, "slow": WRONG_SLOW,
                          "crash": "raise SystemExit(2)"}.items():
            (cls.sols / f"{name}.py").write_text(src)

    def solve(self, name):
        cmd = [sys.executable, str(self.sols / f"{name}.py")]
        return kata.run_tests(kata.KATAS / self.kid, cmd)

    def test_card_for_builds_a_web_exercise(self):
        card = kata.card_for(kata.load_katas()[self.kid], kata.KATAS / self.kid)
        self.assertEqual((card["id"], card["topic"]), (f"kata:{self.kid}", "katas"))
        head, _, body = card["question"].partition("\n")
        self.assertTrue(head.startswith("Kata: Maximum subarray sum - explain how you would"))
        self.assertIn("largest sum of a non-empty contiguous subarray", body)
        self.assertIn("Example 2\nInput:\n3\n-3 -1 -2\nOutput:\n-1", body)
        self.assertTrue(body.endswith("Time limit: 2.0s per test."))
        self.assertIn("- Intended approach: Kadane's algorithm", card["ref"])
        self.assertIn("best = cur = a[0]", card["ref"])  # the reference solution
        self.assertIn("Known wrong approach 2 (fails the tests)", card["ref"])

    def test_export_includes_only_approved_exercises(self):
        pending = kata.PENDING / "later"
        pending.mkdir(parents=True, exist_ok=True)
        (pending / "kata.json").write_text("{}")
        self.addCleanup(shutil.rmtree, pending)
        cards = export_cards.exercise_cards()
        self.assertEqual([c["id"] for c in cards if c["id"].startswith("kata:")],
                         [f"kata:{self.kid}"])
        self.assertEqual(json.loads(json.dumps(cards)), cards)

    def test_review_moved_kata_out_of_pending(self):
        self.assertIn(self.kid, kata.load_katas())
        self.assertEqual(list(kata.PENDING.glob("*/kata.json")), [])

    def test_correct_solution_passes_everything(self):
        results = self.solve("good")
        self.assertTrue(all(r["verdict"] == "OK" for r in results))
        self.assertEqual([r["group"] for r in results][:3], ["ex", "ex", "small"])

    def summary(self, results, reveal=False):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            kata.summarize(results, reveal)
        return out.getvalue()

    def test_example_failures_are_shown_hidden_ones_are_not(self):
        results = self.solve("empty")
        self.assertEqual(next(r for r in results if r["verdict"] != "OK")["verdict"], "WA")
        self.assertIn("expected:", self.summary(results))  # failed a public example
        hidden = [r for r in results if r["group"] != "ex"]
        self.assertIn("hidden test", self.summary(hidden))
        self.assertIn("expected:", self.summary(hidden, reveal=True))

    def test_slow_solution_gets_tle_on_large(self):
        verdicts = {r["name"]: r["verdict"] for r in self.solve("slow")}
        self.assertEqual(verdicts["large1"], "TLE")
        self.assertEqual(verdicts["ex1"], "OK")

    def test_crash_is_runtime_error(self):
        self.assertEqual({r["verdict"] for r in self.solve("crash")}, {"RE"})

    def test_solve_command_records_progress(self):
        cmd = [sys.executable, str(self.sols / "good.py")]
        args = mock.Mock(id=self.kid, reveal=False, cmd=cmd)
        with contextlib.redirect_stdout(io.StringIO()):
            kata.cmd_solve(args)
        db = kata.connect()
        row = db.execute("SELECT passed, total FROM ex_attempts WHERE key = ?",
                         (f"kata:{self.kid}",)).fetchone()
        self.assertEqual(row[0], row[1])
        self.assertIsNone(kata.pick(db, "kata", [self.kid]))  # solved, not due yet


class CliTest(unittest.TestCase):
    def test_everything_after_double_dash_is_the_command(self):
        seen = []
        argv = ["kata.py", "solve", "abc", "--reveal", "--", "./a.out", "--flag", "x"]
        with mock.patch.object(sys, "argv", argv):
            with mock.patch.object(kata, "cmd_solve", seen.append):
                kata.main()
        (args,) = seen
        expected = ("abc", True, ["./a.out", "--flag", "x"])
        self.assertEqual((args.id, args.reveal, args.cmd), expected)


class ProgressTest(Base):
    def test_record_and_pick(self):
        db = kata.connect()
        first = mock.Mock(choice=lambda xs: xs[0])
        self.assertEqual(kata.pick(db, "bug", ["a", "b"], rng=first), "a")
        self.assertEqual(kata.record(db, "bug:a", 5, 4, 4), 1.0)
        self.assertEqual(kata.record(db, "bug:a", 5, 4, 4), 3.0)
        self.assertEqual(kata.pick(db, "bug", ["a", "b"], rng=first), "b")
        self.assertEqual(kata.record(db, "bug:b", 1, 0, 4), 1.0)  # failure resets
        db.execute("UPDATE ex_state SET due = '2000-01-01T00:00:00Z' WHERE key = 'bug:a'")
        self.assertEqual(kata.pick(db, "bug", ["a", "b"]), "a")  # overdue wins


if __name__ == "__main__":
    unittest.main()

import importlib.util
import json
import sys
import unittest
from pathlib import Path

PATH = Path(__file__).parent / "web" / "public" / "py" / "harness.py"
spec = importlib.util.spec_from_file_location("harness", PATH)
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


def run(code, stdin=""):
    return json.loads(harness.run_case(code, stdin))


class HarnessTest(unittest.TestCase):
    def test_reads_stdin_all_ways_and_prints(self):
        code = "import sys\na = input()\nb = sys.stdin.readline()\nc = sys.stdin.read()\n" \
               "print(a, b.strip(), c.split())"
        self.assertEqual(run(code, "x\ny\n1 2\n3\n"), {"rc": 0, "out": "x y ['1', '2', '3']\n",
                                                         "err": ""})

    def test_main_guard_runs_and_namespace_is_fresh(self):
        code = "if __name__ == '__main__':\n    print(globals().get('leak', 'clean'))\nleak = 1\n"
        self.assertEqual(run(code)["out"], "clean\n")
        self.assertEqual(run(code)["out"], "clean\n")  # state does not leak between runs

    def test_exceptions_are_reported_with_a_traceback(self):
        r = run("print('before')\n1 / 0\n")
        self.assertEqual((r["rc"], r["out"]), (1, "before\n"))
        self.assertIn("ZeroDivisionError", r["err"])
        self.assertIn("solution.py", r["err"])

    def test_syntax_errors_are_reported(self):
        r = run("def f(:\n")
        self.assertEqual(r["rc"], 1)
        self.assertIn("SyntaxError", r["err"])

    def test_exit_codes(self):
        self.assertEqual(run("import sys\nsys.exit(0)")["rc"], 0)
        self.assertEqual(run("import sys\nsys.exit()")["rc"], 0)
        self.assertEqual(run("import sys\nsys.exit(3)")["rc"], 3)
        r = run("import sys\nsys.exit('boom')")
        self.assertEqual((r["rc"], r["err"]), (1, "boom\n"))

    def test_streams_and_recursion_limit_are_restored(self):
        before = (sys.stdin, sys.stdout, sys.stderr, sys.getrecursionlimit())
        run("import sys\nsys.setrecursionlimit(50000)\nraise SystemExit(2)")
        run("1/0")
        self.assertEqual((sys.stdin, sys.stdout, sys.stderr, sys.getrecursionlimit()), before)

    def test_deep_recursion_hits_the_default_limit(self):
        r = run("def f(n):\n    return f(n + 1)\nf(0)")
        self.assertEqual(r["rc"], 1)
        self.assertIn("RecursionError", r["err"])

    def test_large_input_is_fast_enough(self):
        n = 200_000
        code = "import sys\nprint(sum(map(int, sys.stdin.read().split())))"
        r = run(code, " ".join(["1"] * n))
        self.assertEqual(r["out"], f"{n}\n")

    def test_stderr_output_is_captured_separately(self):
        r = run("import sys\nprint('o')\nprint('e', file=sys.stderr)")
        self.assertEqual((r["out"], r["err"]), ("o\n", "e\n"))


if __name__ == "__main__":
    unittest.main()

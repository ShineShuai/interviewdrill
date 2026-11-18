import contextlib
import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bugs
import drill
import export_cards
import problems

HAVE_CXX = shutil.which(bugs.CXX) is not None
REAL_GO = shutil.which(bugs.GO) is not None  # evaluated before any test patches PATH

UAF = """#include <iostream>
#include <vector>
int main() {
    std::vector<int> v = {1, 2, 3};
    int* first = &v[0];
    v.push_back(4);
    std::cout << *first << "\\n";
    return 0;
}
"""
UAF_FIXED = UAF.replace("    int* first = &v[0];\n", "").replace("*first", "v[0]")
OVERFLOW = """#include <iostream>
int main() {
    int* a = new int[4];
    for (int i = 0; i <= 4; i++) a[i] = i;
    std::cout << a[3] << "\\n";
    delete[] a;
    return 0;
}
"""
RACE = """#include <iostream>
#include <thread>
int counter = 0;
void work() { for (int i = 0; i < 1000; i++) counter++; }
int main() {
    std::thread a(work), b(work);
    a.join();
    b.join();
    std::cout << counter << "\\n";
    return 0;
}
"""
RACE_FIXED = RACE.replace("#include <thread>", "#include <atomic>\n#include <thread>").replace(
    "int counter = 0;", "std::atomic<int> counter{0};")
SIGNED = """#include <iostream>
#include <limits>
int main() {
    int x = std::numeric_limits<int>::max();
    int y = x + 1;
    std::cout << y << "\\n";
    return 0;
}
"""
SIGNED_FIXED = SIGNED.replace("int y = x + 1;", "long long y = (long long)x + 1;")
LEAK = """#include <iostream>
int main() {
    int* p = new int[10];
    p[0] = 1;
    std::cout << p[0] << "\\n";
    return 0;
}
"""
LEAK_FIXED = LEAK.replace("    return 0;", "    delete[] p;\n    return 0;")
DOUBLE = """#include <iostream>
int main() {
    int* p = new int(5);
    std::cout << *p << "\\n";
    delete p;
    delete p;
    return 0;
}
"""
DOUBLE_FIXED = DOUBLE.replace("    delete p;\n    delete p;\n", "    delete p;\n")

LOCKS = """#include <mutex>
#include <thread>
std::mutex a, b;
int main() {
    std::thread t1([] { std::lock_guard<std::mutex> x(a); std::lock_guard<std::mutex> y(b); });
    t1.join();
    std::thread t2([] { std::lock_guard<std::mutex> x(b); std::lock_guard<std::mutex> y(a); });
    t2.join();
    return 0;
}
"""
LOCKS_FIXED = LOCKS.replace(
    "x(b); std::lock_guard<std::mutex> y(a);", "x(a); std::lock_guard<std::mutex> y(b);"
)


def spec(category, buggy, fixed, title="t"):
    return {"title": title, "category": category, "buggy": buggy, "fixed": fixed,
            "explanation": "Because. Fix it.", "difficulty": 2}


class FirstReportTest(unittest.TestCase):
    def test_extracts_the_first_finding(self):
        cases = {
            "==1==ERROR: AddressSanitizer: heap-use-after-free on 0x1": "heap-use-after-free",
            "==1==ERROR: AddressSanitizer: attempting double-free on 0x1": "double-free",
            "==1==ERROR: LeakSanitizer: detected memory leaks": "detected memory leaks",
            "WARNING: ThreadSanitizer: data race (pid=7)": "data race",
            "WARNING: ThreadSanitizer: lock-order-inversion (potential deadlock) (pid=7)":
                "lock-order-inversion",
            "a.cpp:5:11: runtime error: signed integer overflow: 1 + 1": "signed integer overflow",
        }
        for text, needle in cases.items():
            self.assertIn(needle, bugs.first_report(text))
        self.assertEqual(bugs.first_report("all good"), "")

    def test_denylist(self):
        for bad in ('system("ls");', "#include <fstream>", "fork();", "x = popen(a, b);"):
            self.assertTrue(bugs.DENY.search(bad), bad)
        self.assertFalse(bugs.DENY.search("std::remove(v.begin(), v.end(), 1);"))


@unittest.skipUnless(HAVE_CXX, "needs a C++ compiler with sanitizers")
class VerifyTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.work = Path(tmp.name) / "w"

    def ok(self, category, buggy, fixed):
        report, reason = bugs.verify(spec(category, buggy, fixed), self.work)
        self.assertEqual(reason, "")
        self.assertEqual(report["category"], category)
        self.assertGreaterEqual(report["changed_lines"], 1)
        return report

    def test_each_category_verifies(self):
        for category, buggy, fixed in [
            ("heap-use-after-free", UAF, UAF_FIXED),
            ("heap-buffer-overflow", OVERFLOW, OVERFLOW.replace("new int[4]", "new int[5]")),
            ("data-race", RACE, RACE_FIXED),
            ("signed-overflow", SIGNED, SIGNED_FIXED),
            ("memory-leak", LEAK, LEAK_FIXED),
            ("double-free", DOUBLE, DOUBLE_FIXED),
            ("lock-order-inversion", LOCKS, LOCKS_FIXED),
        ]:
            with self.subTest(category):
                shutil.rmtree(self.work, ignore_errors=True)
                report = self.ok(category, buggy, fixed)
                self.assertTrue(report["excerpt"])
                self.assertIn("@@", report["diff"])

    def test_report_excerpt_is_clean(self):
        report = self.ok("heap-use-after-free", UAF, UAF_FIXED)
        self.assertNotIn(str(self.work), report["excerpt"])
        self.assertNotRegex(report["excerpt"], r"==\d+==")
        self.assertIn("heap-use-after-free", report["excerpt"])
        self.assertNotIn("BuildId", report["excerpt"])  # nor frames of libc and program start
        self.assertNotRegex(report["excerpt"], r"libc|_start")

    def reject(self, category, buggy, fixed, needle):
        report, reason = bugs.verify(spec(category, buggy, fixed), self.work)
        self.assertIsNone(report)
        self.assertIn(needle, reason)

    def test_rejects_wrong_category(self):
        self.reject("data-race", UAF, UAF_FIXED, "expected a 'data race' report")

    def test_rejects_program_without_bug(self):
        self.reject("heap-use-after-free", UAF_FIXED, UAF_FIXED, "got 'none'")

    def test_rejects_fix_that_still_fails(self):
        self.reject("heap-use-after-free", UAF, UAF, "fixed is not clean")

    def test_rejects_large_diff(self):
        padded = UAF_FIXED + "".join(f"// note {i}\n" for i in range(20))
        self.reject("heap-use-after-free", UAF, padded, "small diff")

    def test_rejects_compile_error_and_forbidden_calls(self):
        self.reject("heap-use-after-free", "int main( {", UAF_FIXED, "does not compile")
        evil = UAF.replace("return 0;", 'system("true"); return 0;')
        self.reject("heap-use-after-free", evil, UAF_FIXED, "forbidden")

    def test_rejects_malformed(self):
        self.reject("heap-use-after-free", "", UAF_FIXED, "missing buggy")
        self.reject("nope", UAF, UAF_FIXED, "unknown category")


@unittest.skipUnless(HAVE_CXX, "needs a C++ compiler with sanitizers")
class PipelineTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for target, name, value in [
            (bugs, "BUGS", root / "bugs"), (bugs, "PENDING", root / "bugs" / "_pending"),
            (drill, "HOME", root / "home"), (drill, "CARDS", root / "cards"),
            (problems, "PROBLEMS", root / "problems"),
            (problems, "PENDING", root / "problems" / "_pending"),
        ]:
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        (root / "cards").mkdir()

    def test_repair_loop_review_and_practice(self):
        calls = []

        def propose(category, difficulty, avoid, prev, feedback):
            calls.append(feedback)
            return spec("data-race", "int main( {", "int main() {}") if len(calls) == 1 else \
                spec("heap-use-after-free", UAF, UAF_FIXED, "Dangling vector pointer")

        work, reason = bugs.make_bug("heap-use-after-free", 2, [], propose)
        self.assertEqual(reason, "")
        self.assertIn("does not compile", calls[1])
        meta = json.loads((work / "bug.json").read_text())
        self.assertEqual(meta["category"], "heap-use-after-free")  # requested category wins

        quiet = contextlib.redirect_stdout(io.StringIO())
        with mock.patch("builtins.input", side_effect=["a"]), quiet:
            self.assertEqual(dict(bugs.review()), {"approved": 1})
        self.assertEqual(list(bugs.load_bugs()), [meta["id"]])
        card = bugs.card_for(bugs.load_bugs()[meta["id"]], bugs.BUGS / meta["id"])
        self.assertEqual((card["id"], card["topic"]), (f"bug:{meta['id']}", "bugs"))
        self.assertTrue(card["question"].startswith("Find the single bug in this C++ program"))
        self.assertIn("  6 |     v.push_back(4);", card["question"])
        self.assertIn("Bug class: heap-use-after-free", card["ref"])
        self.assertIn(card, export_cards.exercise_cards())

        grade = {"correct": 4, "depth": 3, "clarity": 5, "missed": [], "errors": [],
                 "followup": "?"}
        seen = {}

        def judge(card, answer):
            seen.update(card=card, answer=answer)
            return grade

        out = io.StringIO()
        with mock.patch.object(drill, "read_answer", return_value="push_back reallocates"), \
                mock.patch.object(drill, "grade", judge), contextlib.redirect_stdout(out):
            self.assertTrue(bugs.practice(problems.connect()))
        self.assertIn("heap-use-after-free", seen["card"]["ref"])
        self.assertIn("-    int* first", seen["card"]["ref"])
        self.assertIn("v.push_back(4);", seen["card"]["question"])
        self.assertIn("ground truth", out.getvalue())
        row = problems.connect().execute("SELECT key FROM ex_attempts").fetchone()
        self.assertEqual(row[0], f"bug:{meta['id']}")

    def test_gives_up_and_cleans_up(self):
        bad = spec("heap-use-after-free", UAF_FIXED, UAF_FIXED)
        work, reason = bugs.make_bug("heap-use-after-free", 2, [], lambda *a: bad, repairs=1)
        self.assertIsNone(work)
        self.assertIn("got 'none'", reason)
        self.assertEqual(list(bugs.PENDING.glob("*")), [])


GO_RACE = """package main

import (
	"fmt"
	"sync"
)

func main() {
	counter := 0
	var wg sync.WaitGroup
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 1000; j++ {
				counter++
			}
		}()
	}
	wg.Wait()
	fmt.Println(counter)
}
"""
GO_RACE_FIXED = GO_RACE.replace(
    "	counter := 0\n", "	counter := 0\n	var mu sync.Mutex\n").replace(
    "				counter++\n", "				mu.Lock()\n				counter++\n				mu.Unlock()\n")
GO_DEADLOCK = """package main

import "fmt"

func main() {
	ch := make(chan int)
	ch <- 1
	fmt.Println(<-ch)
}
"""
GO_DEADLOCK_FIXED = GO_DEADLOCK.replace("make(chan int)", "make(chan int, 1)")
GO_NIL_MAP = """package main

import "fmt"

func main() {
	var counts map[string]int
	counts["a"]++
	fmt.Println(counts)
}
"""
GO_NIL_MAP_FIXED = GO_NIL_MAP.replace("var counts map[string]int", "counts := map[string]int{}")
GO_CLOSED = """package main

import "fmt"

func main() {
	ch := make(chan int, 1)
	close(ch)
	ch <- 1
	fmt.Println("done")
}
"""
GO_CLOSED_FIXED = GO_CLOSED.replace("	close(ch)\n	ch <- 1\n", "	ch <- 1\n	close(ch)\n")
GO_CASES = [
    ("go-data-race", GO_RACE, GO_RACE_FIXED),
    ("go-deadlock", GO_DEADLOCK, GO_DEADLOCK_FIXED),
    ("go-nil-map-write", GO_NIL_MAP, GO_NIL_MAP_FIXED),
    ("go-closed-channel", GO_CLOSED, GO_CLOSED_FIXED),
]

# A stand-in `go` for machines without Go: `go build` writes a shell "binary" whose behaviour is
# chosen by FAKE_* markers in the source. It also checks the flags and environment that
# bugs.verify must pass (module dir, -race, CGO_ENABLED, no proxy / toolchain download).
FAKE_GO = r"""#!/usr/bin/env python3
import os, sys
a = sys.argv[1:]
if a[:1] != ["build"] or not os.path.exists("go.mod"):
    sys.stderr.write("fake go: expected `go build` inside a module directory\n")
    sys.exit(3)
out, src, race = a[a.index("-o") + 1], a[-1], "-race" in a
if os.environ.get("GOPROXY") != "off" or os.environ.get("GOTOOLCHAIN") != "local":
    sys.stderr.write("fake go: unsafe environment\n")
    sys.exit(4)
if os.environ.get("CGO_ENABLED") != ("1" if race else "0"):
    sys.stderr.write("fake go: wrong CGO_ENABLED\n")
    sys.exit(5)
text = open(src).read()
if "SYNTAX_ERROR" in text:
    sys.stderr.write(src + ":1:1: syntax error\n")
    sys.exit(2)
body = "echo ok"
if "FAKE_RACE" in text and race:
    body = ("printf '==================\nWARNING: DATA RACE\n"
            "Write at 0x00c000012345 by goroutine 7:\n' >&2; exit 66")
elif "FAKE_DEADLOCK" in text:
    body = "echo 'fatal error: all goroutines are asleep - deadlock!' >&2; exit 2"
elif "FAKE_NILMAP" in text:
    body = "echo 'panic: assignment to entry in nil map' >&2; exit 2"
elif "FAKE_CLOSED" in text:
    body = "echo 'panic: send on closed channel' >&2; exit 2"
open(out, "w").write("#!/bin/sh\n" + body + "\n")
os.chmod(out, 0o755)
"""


class GoReportTest(unittest.TestCase):
    def test_first_report_for_go_output(self):
        cases = {
            "==================\nWARNING: DATA RACE\nWrite at 0x00c0000b4010": "DATA RACE",
            "fatal error: all goroutines are asleep - deadlock!\n\ngoroutine 1 [chan send]:":
                "all goroutines are asleep - deadlock",
            "panic: assignment to entry in nil map\n\ngoroutine 1 [running]:":
                "assignment to entry in nil map",
            "panic: send on closed channel": "closed channel",
            "panic: close of closed channel": "closed channel",
            "panic: runtime error: index out of range [5] with length 3": "index out of range",
        }
        for text, needle in cases.items():
            with self.subTest(needle):
                self.assertIn(needle, bugs.first_report(text, "go"))
        self.assertEqual(bugs.first_report("3000\n", "go"), "")
        self.assertEqual(bugs.first_report("WARNING: DATA RACE", "cpp"), "")  # language-specific

    def test_go_denylist(self):
        for bad in ('import "os/exec"', 'import (\n\t"fmt"\n\t"net/http"\n)', 'import "C"',
                    'os.Remove("x")', 'os.WriteFile("a", nil, 0)', "//go:linkname a b",
                    'import "unsafe"'):
            self.assertTrue(bugs.DENY_GO.search(bad), bad)
        ok = 'import (\n\t"fmt"\n\t"sync"\n\t"os"\n)\nfunc main() { os.Exit(1); fmt.Println(1) }'
        self.assertFalse(bugs.DENY_GO.search(ok))

    def test_build_command_and_environment(self):
        race, plain = bugs.CATEGORIES["go-data-race"], bugs.CATEGORIES["go-deadlock"]
        src, out = Path("/w/buggy.go"), Path("/w/buggy")
        self.assertEqual(bugs.build_cmd(race, src, out),
                         [bugs.GO, "build", "-race", "-o", "/w/buggy", "/w/buggy.go"])
        self.assertNotIn("-race", bugs.build_cmd(plain, src, out))
        self.assertEqual(bugs.build_env(race)["CGO_ENABLED"], "1")
        self.assertEqual(bugs.build_env(plain)["CGO_ENABLED"], "0")
        for cat in (race, plain):
            env = bugs.build_env(cat)
            self.assertEqual((env["GOPROXY"], env["GOTOOLCHAIN"]), ("off", "local"))
        self.assertEqual(bugs.build_env(bugs.CATEGORIES["data-race"]), {})

    def test_ground_truth_skips_separator_lines(self):
        self.assertEqual(bugs.report_line("==================\nWARNING: DATA RACE\nx"),
                         "WARNING: DATA RACE")
        self.assertEqual(bugs.report_line(""), "")
        meta = {"category": "go-data-race", "explanation": "e",
                "provenance": {"verified": {"excerpt": "=====\nWARNING: DATA RACE", "diff": "d"}}}
        self.assertIn("- Tool report: WARNING: DATA RACE", bugs.ground_truth(meta))

    def test_legacy_metadata_is_cpp(self):
        self.assertEqual(bugs.lang_of({}).ext, "cpp")
        self.assertEqual(bugs.lang_of({"lang": "go"}).ext, "go")

    def test_every_category_has_a_known_language_and_prompt(self):
        for name, cat in bugs.CATEGORIES.items():
            self.assertIn(cat.lang, bugs.LANGS, name)
            self.assertIn(cat.lang, bugs.PROPOSE_SYSTEM, name)
            self.assertTrue(cat.signature, name)


class FakeGoBase(unittest.TestCase):
    """Runs the Go code path against FAKE_GO placed first on PATH."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        fake = self.root / "bin"
        fake.mkdir()
        (fake / "go").write_text(FAKE_GO)
        (fake / "go").chmod((fake / "go").stat().st_mode | stat.S_IXUSR)
        patcher = mock.patch.dict(os.environ, {"PATH": f"{fake}:{os.environ['PATH']}"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.work = self.root / "w"

    def verify(self, category, buggy, fixed):
        return bugs.verify(spec(category, buggy, fixed), self.work)


class FakeGoVerifyTest(FakeGoBase):
    def test_race_deadlock_nilmap_and_closed_channel_verify(self):
        for category, marker in [("go-data-race", "FAKE_RACE"), ("go-deadlock", "FAKE_DEADLOCK"),
                                 ("go-nil-map-write", "FAKE_NILMAP"),
                                 ("go-closed-channel", "FAKE_CLOSED")]:
            with self.subTest(category):
                shutil.rmtree(self.work, ignore_errors=True)
                buggy = f"package main\n// {marker}\nfunc main() {{ a() }}\n"
                fixed = "package main\nfunc main() { b() }\n"
                report, reason = self.verify(category, buggy, fixed)
                self.assertEqual(reason, "")
                self.assertEqual(report["category"], category)
                self.assertIn("buggy.go", report["diff"])
                self.assertTrue((self.work / "go.mod").exists())
                self.assertTrue((self.work / "buggy.go").exists())

    def test_race_report_excerpt_skips_the_separator(self):
        report, _ = self.verify("go-data-race", "// FAKE_RACE\npackage main\n", "package main\n")
        self.assertEqual(bugs.report_line(report["excerpt"]), "WARNING: DATA RACE")
        self.assertNotIn("0x00c000012345", report["excerpt"])  # addresses are normalized

    def test_race_needs_the_race_flag(self):
        # the fake only reports a race when built with -race; other categories never get it
        _, reason = self.verify("go-deadlock", "// FAKE_RACE\npackage main\n", "package main\n")
        self.assertIn("expected a 'all goroutines are asleep - deadlock' report", reason)
        self.assertIn("got 'none'", reason)

    def test_rejections(self):
        def reject(category, buggy, fixed, needle):
            shutil.rmtree(self.work, ignore_errors=True)
            report, reason = self.verify(category, buggy, fixed)
            self.assertIsNone(report)
            self.assertIn(needle, reason)

        ok = "package main\n"
        reject("go-data-race", "// SYNTAX_ERROR\n" + ok, ok, "buggy does not compile")
        reject("go-data-race", "// FAKE_RACE\n" + ok, "// SYNTAX_ERROR\n" + ok,
               "fixed does not compile")
        reject("go-data-race", "// FAKE_RACE\n" + ok, "// FAKE_RACE\n" + ok, "fixed is not clean")
        reject("go-data-race", ok, ok, "got 'none'")
        reject("go-data-race", 'import "os/exec"\n// FAKE_RACE\n', ok, "forbidden")
        big = ok + "".join(f"// line {i}\n" for i in range(20))
        reject("go-data-race", "// FAKE_RACE\n" + ok, big, "small diff")

    def test_missing_toolchain_is_reported(self):
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            report, reason = self.verify("go-deadlock", "package main\n", "package main\n")
            self.assertIsNone(report)
            self.assertIn("not found on PATH", reason)
            self.assertFalse([c for c in bugs.available() if c.startswith("go-")])
        self.assertIn("go-deadlock", bugs.available())


class FakeGoPipelineTest(FakeGoBase):
    def test_go_exercise_flows_through_review_card_and_export(self):
        for target, name, value in [
            (bugs, "BUGS", self.root / "bugs"), (bugs, "PENDING", self.root / "bugs" / "_pending"),
            (drill, "HOME", self.root / "home"), (drill, "CARDS", self.root / "cards"),
            (problems, "PROBLEMS", self.root / "problems"),
            (problems, "PENDING", self.root / "problems" / "_p"),
        ]:
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        (self.root / "cards").mkdir()
        buggy = "package main\n// FAKE_DEADLOCK\n\nfunc main() {\n\tch := make(chan int)\n}\n"
        fixed = buggy.replace("make(chan int)", "make(chan int, 1)")
        fixed = fixed.replace("// FAKE_DEADLOCK\n", "")
        proposal = spec("go-deadlock", buggy, fixed, "Unbuffered send")
        work, reason = bugs.make_bug("go-deadlock", 2, [], lambda *a: proposal)
        self.assertEqual(reason, "")
        meta = json.loads((work / "bug.json").read_text())
        self.assertEqual((meta["lang"], meta["category"]), ("go", "go-deadlock"))
        quiet = contextlib.redirect_stdout(io.StringIO())
        with mock.patch("builtins.input", side_effect=["a"]), quiet:
            self.assertEqual(dict(bugs.review()), {"approved": 1})
        card = export_cards.exercise_cards()[0]
        self.assertEqual((card["id"], card["topic"]), (f"bug:{meta['id']}", "bugs"))
        self.assertTrue(card["question"].startswith("Find the single bug in this Go program"))
        self.assertIn("  5 | \tch := make(chan int)", card["question"])
        self.assertIn("Bug class: go-deadlock", card["ref"])
        self.assertIn("- Tool report: fatal error: all goroutines are asleep", card["ref"])
        self.assertIn("buggy.go", card["ref"])

    def test_gen_picks_only_categories_with_a_toolchain(self):
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            self.assertEqual(bugs.available(), [])
            with self.assertRaises(SystemExit):
                bugs.cmd_gen(mock.Mock(category=None, difficulty=2, n=1))


@unittest.skipUnless(REAL_GO, "needs a Go toolchain (and a C compiler for -race)")
class RealGoTest(unittest.TestCase):
    """Runs the real race detector and runtime; skipped where Go is not installed."""

    def test_each_go_category_verifies(self):
        for category, buggy, fixed in GO_CASES:
            with self.subTest(category), tempfile.TemporaryDirectory() as tmp:
                report, reason = bugs.verify(spec(category, buggy, fixed), Path(tmp) / "w")
                self.assertEqual(reason, "")
                self.assertEqual(report["category"], category)
                self.assertIn("@@", report["diff"])


if __name__ == "__main__":
    unittest.main()

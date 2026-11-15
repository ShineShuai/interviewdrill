import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import bugs
import drill
import export_cards
import kata

HAVE_CXX = shutil.which(bugs.CXX) is not None

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
            (kata, "KATAS", root / "katas"), (kata, "PENDING", root / "katas" / "_pending"),
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
                mock.patch.object(drill, "judge", judge), contextlib.redirect_stdout(out):
            self.assertTrue(bugs.practice(kata.connect()))
        self.assertIn("heap-use-after-free", seen["card"]["ref"])
        self.assertIn("-    int* first", seen["card"]["ref"])
        self.assertIn("v.push_back(4);", seen["card"]["question"])
        self.assertIn("ground truth", out.getvalue())
        row = kata.connect().execute("SELECT key FROM ex_attempts").fetchone()
        self.assertEqual(row[0], f"bug:{meta['id']}")

    def test_gives_up_and_cleans_up(self):
        bad = spec("heap-use-after-free", UAF_FIXED, UAF_FIXED)
        work, reason = bugs.make_bug("heap-use-after-free", 2, [], lambda *a: bad, repairs=1)
        self.assertIsNone(work)
        self.assertIn("got 'none'", reason)
        self.assertEqual(list(bugs.PENDING.glob("*")), [])


if __name__ == "__main__":
    unittest.main()

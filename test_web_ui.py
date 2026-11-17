"""Browser tests for the web app: real Chromium (Playwright) against web/test/ui-server.ts.

The server runs the real Worker on an in-memory D1 with the real cards and the real security
headers; only Workers AI and Pyodide are stand-ins. Skipped when Playwright, Chromium or
Node 22+ is not available.
"""
import contextlib
import os
import re
import socket
import subprocess
import unittest
from datetime import datetime, timezone
from pathlib import Path

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

WEB = Path(__file__).parent / "web"
MOBILE = {"width": 375, "height": 667}
CHROMIUM_ARGS = ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"]
COUNTS = {f.stem: len(re.findall(r"^## ", f.read_text(), re.M))
          for f in sorted((Path(__file__).parent / "cards").glob("*.md"))}
COUNTS["problems"] = 1  # the stand-in problem that ui-server.ts adds
TOTAL = sum(COUNTS.values())


def now_z():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


SOLUTION = "a, b = map(int, input().split())\nprint(a + b)"


@contextlib.contextmanager
def serve(**env):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(["node", "test/ui-server.ts", str(port)], cwd=WEB,
                            env={**os.environ, **env}, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        if proc.stdout.readline().strip() != "ready":
            raise RuntimeError("ui-server did not start")
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        proc.wait()
        proc.stdout.close()


def setUpModule():
    global PW, BROWSER
    if sync_playwright is None:
        raise unittest.SkipTest("playwright is not installed")
    PW = sync_playwright().start()
    try:
        BROWSER = PW.chromium.launch(args=CHROMIUM_ARGS)
    except Exception as e:  # noqa: BLE001
        PW.stop()
        raise unittest.SkipTest(f"chromium is not available: {e}")


def tearDownModule():
    BROWSER.close()
    PW.stop()


class UiCase(unittest.TestCase):
    server_env: dict = {}

    def setUp(self):
        # a fresh server (empty in-memory database) per test: tests do not share attempts
        serving = serve(**self.server_env)
        self.base = serving.__enter__()
        self.addCleanup(serving.__exit__, None, None, None)
        self.ctx = BROWSER.new_context(viewport=MOBILE, permissions=["microphone"])
        self.addCleanup(self.ctx.close)
        self.page = self.ctx.new_page()
        self.problems = []
        self.page.on("console", lambda m: m.type == "error" and self.problems.append(m.text))
        self.page.on("pageerror", lambda e: self.problems.append(str(e)))
        self.page.goto(self.base)

    def tearDown(self):
        self.assertEqual(self.problems, [], "console errors or CSP violations")

    def no_overflow(self):
        wide = self.page.evaluate(
            "[...document.querySelectorAll('body *')].filter(e => e.getBoundingClientRect().right"
            " > innerWidth + 1).map(e => e.tagName + ':' + e.textContent.slice(0, 30))")
        self.assertEqual(wide, [])
        self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), 375)

    def start(self, topic="", n=1):
        self.page.select_option("select", topic)
        self.page.fill("input[type=number]", str(n))
        self.page.click("text=Start")


class GradedTest(UiCase):
    def test_home_lists_due_cards_and_the_grader(self):
        p = self.page
        expect(p.locator("h1")).to_have_text(f"{TOTAL} due")
        expect(p.locator("body")).not_to_contain_text("null")
        expect(p.locator("body")).to_contain_text("Graded by workers-ai")
        self.assertIn(f"go ({COUNTS['go']})", p.locator("select").inner_text())
        self.no_overflow()

    def test_drill_two_cards_with_a_review_between(self):
        p = self.page
        self.start("go", 2)
        expect(p.locator("p.dim").first).to_contain_text("1/2 · go")
        self.no_overflow()
        p.fill("textarea", "my claim")
        p.click("button.primary")  # Submit
        expect(p.locator("h2")).to_have_text("3.9 / 5")
        expect(p.locator("body")).to_contain_text("the missed point")
        expect(p.locator("body")).to_contain_text('"my claim" — it is wrong')
        expect(p.locator("body")).to_contain_text("Follow-up: Why is that?")
        p.click("summary")
        self.assertTrue(p.locator("details pre").inner_text().startswith("- "))
        self.no_overflow()
        p.get_by_role("button", name="Next", exact=True).click()
        expect(p.locator("p.dim").first).to_contain_text("2/2 · go")
        p.fill("textarea", "second")
        p.press("textarea", "Control+Enter")
        p.click("text=Finish")
        expect(p.locator("h1")).to_have_text(f"{TOTAL - 2} due")
        expect(p.locator("body")).to_contain_text("2 answered today")

    def test_empty_answer_is_not_submitted(self):
        self.start("go")
        self.page.click("button.primary")
        expect(self.page.locator("textarea")).to_be_visible()
        expect(self.page.locator("h2").first).not_to_contain_text("/ 5")

    def test_report_tab(self):
        p = self.page
        p.click("nav >> text=Report")
        expect(p.locator("body")).to_contain_text("No attempts yet.")
        p.click("nav >> text=Drill")
        self.start("go")
        p.fill("textarea", "x")
        p.click("button.primary")
        p.click("text=Finish")
        p.click("nav >> text=Report")
        expect(p.locator("table")).to_contain_text("go")
        expect(p.locator("a[download]")).to_have_attribute("href", "/api/export")
        self.no_overflow()

    def test_voice_fills_the_answer_box(self):
        p = self.page
        self.start("go")
        p.click("text=Record")
        expect(p.locator("text=Stop")).to_be_visible()
        p.wait_for_timeout(1200)
        p.click("text=Stop")
        expect(p.locator("textarea")).to_have_value("spoken words", timeout=15000)
        expect(p.locator("text=Record")).to_be_visible()

    def test_problem_runner_passes_and_fails(self):
        p = self.page
        self.start("problems")
        expect(p.locator("pre.q")).to_contain_text("Read a and b")
        p.fill("textarea", SOLUTION)
        p.click("text=Run as Python")
        expect(p.locator(".panel h3")).to_have_text("2/2 passed", timeout=15000)
        p.fill("textarea", "print(0)")
        p.click("text=Run as Python")
        expect(p.locator(".panel h3")).to_have_text("0/2 passed", timeout=15000)
        expect(p.locator(".panel")).to_contain_text("first failure: ex1 WA")
        expect(p.locator(".panel pre")).to_contain_text("expected: 3")
        expect(p.locator(".panel")).to_contain_text("next review in")
        p.fill("textarea", "print(1/0)")
        p.click("text=Run as Python")
        expect(p.locator(".panel pre").first).to_contain_text("ZeroDivisionError", timeout=15000)
        self.no_overflow()

    def test_runner_has_no_network(self):
        p = self.page
        self.start("problems")
        p.fill("textarea", "import js\nprint(js.fetch('/api/export'))")
        p.click("text=Run as Python")
        expect(p.locator(".panel h3")).to_have_text("0/2 passed", timeout=15000)

    def test_csp_in_the_page(self):
        p = self.page
        res = p.request.get(self.base + "/")
        self.assertIn("default-src 'none'", res.headers["content-security-policy"])
        self.assertEqual(res.headers["x-content-type-options"], "nosniff")
        p.goto(self.base + "/__csp.html")
        p.wait_for_function("window.__csp")
        got = p.evaluate("window.__csp")
        self.assertEqual({k: got[k] for k in got if k != "violated"},
                         {"eval": "EvalError", "newFunction": "EvalError", "wasm": "ok",
                          "fetchOtherOrigin": "blocked"})
        self.assertIn("connect-src", got["violated"])
        self.assertEqual(p.evaluate(
            "new Promise(r => { const s = document.createElement('script');"
            " s.textContent = 'window.hit = 1'; document.head.append(s);"
            " setTimeout(() => r(window.hit ?? 0), 300); })"), 0)  # inline script is blocked
        self.problems.clear()  # blocked probes are logged as console errors on purpose

    def test_csp_in_a_worker(self):
        # the Pyodide worker gets the CSP of its own response: wasm compiles, eval and network do not
        got = self.page.evaluate(
            "new Promise(r => { const w = new Worker('/__csp.js'); w.onmessage = e => r(e.data);"
            " w.onerror = e => r({ error: e.message }); })")
        self.assertEqual({k: got[k] for k in got if k != "violated"},
                         {"eval": "EvalError", "newFunction": "EvalError", "wasm": "ok",
                          "fetchOtherOrigin": "blocked"})
        self.problems.clear()

    def test_due_cards_are_cached_with_their_references(self):
        self.wait_for_cache()
        cards = self.page.evaluate("JSON.parse(localStorage.getItem('drill.cards'))")
        self.assertTrue(0 < len(cards) <= 30)
        for c in cards:
            self.assertTrue(c["ref"] and c["points"] and c["question"])
            self.assertFalse(c["id"].startswith("problem:"))

    def test_offline_drill_queues_attempts_and_syncs_them_when_back_online(self):
        p = self.page
        self.go_offline()
        cached = len(self.queue("drill.cards"))
        expect(p.locator("h1")).to_have_text(f"Offline · {cached} cards")
        expect(p.locator("body")).not_to_contain_text("null")
        self.start("", 2)  # the cache holds a sample of every topic, so do not filter
        before = p.evaluate("new Date().toISOString()")
        for i in range(2):
            p.fill("textarea", f"offline answer {i}")
            p.click("text=Grade myself")
            expect(p.locator("h2")).to_have_text("Check yourself")
            p.locator("input[type=checkbox]").first.check()
            self.no_overflow()
            p.click("text=Save")
            expect(p.locator("body")).to_contain_text("saved on this device")
            if i == 0:
                p.get_by_role("button", name="Next", exact=True).click()
        p.click("text=Finish")
        expect(p.locator("h1")).to_have_text(f"Offline · {cached - 2} cards")
        expect(p.locator("body")).to_contain_text("2 answers wait to sync")
        queued = self.queue()
        self.assertEqual([q["answer"] for q in queued], ["offline answer 0", "offline answer 1"])
        self.assertEqual(len({q["uid"] for q in queued}), 2)
        self.problems.clear()
        self.ctx.set_offline(False)  # the browser fires "online": the queue is sent by itself
        self.wait_until("JSON.parse(localStorage.getItem('drill.queue') || '[]').length === 0")
        out = self.exported()
        self.assertEqual(sorted(a["answer"] for a in out["attempts"]),
                         ["offline answer 0", "offline answer 1"])
        for a in out["attempts"]:  # kept the time of the answer, not the time of the sync
            self.assertGreaterEqual(a["ts"], before[:19] + "Z")
            self.assertEqual(a["score"], a["correct"] and a["score"])
        self.assertEqual(len(out["card_state"]), 2)
        p.reload()
        expect(p.locator("body")).to_contain_text("2 answered today")
        expect(p.locator("body")).not_to_contain_text("wait to sync")

    def test_a_save_that_fails_online_is_queued_not_lost(self):
        p = self.page
        p.route("**/api/self-grade", lambda r: r.abort())
        self.start("go")
        p.fill("textarea", "will be queued")
        p.click("text=Grade myself")
        p.locator("input[type=checkbox]").first.check()
        p.click("text=Save")
        expect(p.locator("body")).to_contain_text("saved on this device")
        self.assertEqual([q["answer"] for q in self.queue()], ["will be queued"])
        p.unroute("**/api/self-grade")
        p.reload()  # home() sends the queue before it asks for the status
        expect(p.locator("body")).to_contain_text("1 answered today")
        self.assertEqual(self.queue(), [])
        self.assertEqual([a["answer"] for a in self.exported()["attempts"]], ["will be queued"])
        self.problems.clear()  # the aborted request is logged by the browser

    def test_a_refused_queue_item_is_set_aside_and_the_rest_still_syncs(self):
        p = self.page
        good = {"uid": "pwa:good", "key": "go-chan", "ts": now_z(), "q": 4,
                "secs": 3, "answer": "mine", "mode": "offline", "correct": 4, "depth": 4,
                "clarity": 4}
        bad = {**good, "uid": "pwa:bad", "key": "has space"}
        p.evaluate("q => localStorage.setItem('drill.queue', JSON.stringify(q))", [bad, good])
        p.reload()
        expect(p.locator("body")).to_contain_text("1 answered today")
        self.assertEqual(self.queue(), [])
        self.assertEqual([q["uid"] for q in self.queue("drill.rejected")], ["pwa:bad"])
        self.assertEqual([a["answer"] for a in self.exported()["attempts"]], ["mine"])
        self.problems.clear()  # the refused request is logged by the browser

    def test_a_queue_that_cannot_be_sent_stays_for_the_next_time(self):
        p = self.page
        item = {"uid": "pwa:later", "key": "go-chan", "ts": now_z(), "q": 4,
                "secs": 3, "answer": "later", "mode": "offline"}
        p.evaluate("q => localStorage.setItem('drill.queue', JSON.stringify(q))", [item])
        p.route("**/api/sync/push", lambda r: r.fulfill(status=503, body="{}"))
        p.reload()
        expect(p.locator("body")).to_contain_text("1 answers wait to sync")
        self.assertEqual(len(self.queue()), 1)
        self.problems.clear()

    def go_offline(self, forget_cards=False):
        p = self.page
        p.evaluate("navigator.serviceWorker.ready.then(() => true)")
        p.reload()  # now controlled by the worker
        self.wait_until("navigator.serviceWorker.controller !== null")
        self.wait_for_cache()
        p.wait_for_load_state("networkidle")  # a refresh of the cache may still be running
        if forget_cards:
            p.evaluate("localStorage.clear()")
        self.ctx.set_offline(True)
        p.reload()
        self.problems.clear()  # the browser logs the requests that fail offline

    def wait_until(self, js, seconds=10):
        """Poll an expression with evaluate: wait_for_function would eval it under the page's CSP."""
        for _ in range(int(seconds * 10)):
            if self.page.evaluate(js):
                return
            self.page.wait_for_timeout(100)
        self.fail(f"timed out waiting for: {js}")

    def wait_for_cache(self):
        self.wait_until("JSON.parse(localStorage.getItem('drill.cards') || '[]').length > 0")

    def queue(self, key="drill.queue"):
        return self.page.evaluate(f"JSON.parse(localStorage.getItem('{key}') || '[]')")

    def exported(self):
        return self.page.request.get(self.base + "/api/export").json()

    def test_installable_and_works_offline(self):
        p = self.page
        self.go_offline(forget_cards=True)  # nothing cached: the shell loads, the data does not
        expect(p.locator("nav")).to_be_visible()
        expect(p.locator("p.err")).to_have_text("Offline or session expired. Reload.")
        self.ctx.set_offline(False)
        self.problems.clear()  # the failed offline fetch is logged by the browser
        manifest = p.request.get(self.base + "/manifest.webmanifest").json()
        self.assertEqual(manifest["display"], "standalone")


class SelfGradeTest(UiCase):
    server_env = {"UI_LLM": "none"}

    def test_grade_myself_flow(self):
        p = self.page
        expect(p.locator("body")).to_contain_text("You grade yourself")
        self.start("go")
        expect(p.locator("button.primary")).to_have_count(0)  # no model: no Submit button
        p.fill("textarea", "my answer")
        p.click("text=Grade myself")
        expect(p.locator("h2")).to_have_text("Check yourself")
        boxes = p.locator("input[type=checkbox]")
        self.assertGreaterEqual(boxes.count(), 2)
        boxes.nth(0).check()
        boxes.nth(1).check()
        self.no_overflow()
        p.click("text=Save")
        expect(p.locator("h2")).to_contain_text("/ 5")
        p.click("text=Finish")
        expect(p.locator("body")).to_contain_text("1 answered today")

    def test_ctrl_enter_opens_the_self_check(self):
        self.start("go")
        self.page.press("textarea", "Control+Enter")
        expect(self.page.locator("h2")).to_have_text("Check yourself")


class GraderDownTest(UiCase):
    server_env = {"UI_AI_FAIL": "1"}

    def test_error_shown_and_self_grade_still_works(self):
        p = self.page
        self.start("go")
        p.fill("textarea", "answer")
        p.click("button.primary")
        expect(p.locator("p.err")).to_contain_text('grader unavailable')
        expect(p.locator("p.err")).to_contain_text('You can still use "Grade myself"')
        self.problems.clear()  # the 502 is logged as a console error by the browser
        p.click("text=Grade myself")
        expect(p.locator("h2")).to_have_text("Check yourself")


if __name__ == "__main__":
    unittest.main()

"""Browser tests for the web app: real Chromium (Playwright) against web/test/ui-server.ts.

The server runs the real Worker on an in-memory D1 with the real cards and the real security
headers; Workers AI is a stand-in, and so is Pyodide except in RealPyodideTest (needs
`npm install` in web/). WorkerdTest runs the same browser against the real runtime instead
(`wrangler dev`: workerd, local D1, real asset layer and _headers; needs `npm install` in web/ and
about 20 s per test to boot). Skipped when Playwright, Chromium or Node 22+ is not available.
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


@contextlib.contextmanager
def workerd(**_env):
    """The real Worker in workerd (scripts/workerd-server.ts), LLM \"none\", the real cards only."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(["node", "scripts/workerd-server.ts", str(port)], cwd=WEB,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        if proc.stdout.readline().strip() != "ready":
            raise RuntimeError("workerd did not start")
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()  # the script stops wrangler and removes its files; a closed stdin does the same
        proc.wait()
        proc.stdout.close()
        proc.stdin.close()


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
    launch = staticmethod(serve)

    def setUp(self):
        # a fresh server (empty database) per test: tests do not share attempts
        serving = self.launch(**self.server_env)
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

    def press(self, name):
        """Click the button called exactly `name`: text= would also match card text ("AV1 saves ...")."""
        self.page.get_by_role("button", name=name, exact=True).click()

    def start(self, topic="", n=1):
        self.page.select_option("select", topic)
        self.page.fill("input[type=number]", str(n))
        self.press("Start")

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
        self.press("Finish")
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
        self.press("Finish")
        p.click("nav >> text=Report")
        expect(p.locator("table")).to_contain_text("go")
        expect(p.locator("a[download]")).to_have_attribute("href", "/api/export")
        self.no_overflow()

    def test_voice_fills_the_answer_box(self):
        p = self.page
        self.start("go")
        self.press("Record")
        expect(p.get_by_role("button", name="Stop", exact=True)).to_be_visible()
        p.wait_for_timeout(1200)
        self.press("Stop")
        expect(p.locator("textarea")).to_have_value("spoken words", timeout=15000)
        expect(p.get_by_role("button", name="Record", exact=True)).to_be_visible()

    def test_problem_runner_passes_and_fails(self):
        p = self.page
        self.start("problems")
        expect(p.locator("pre.q")).to_contain_text("Read a and b")
        p.fill("textarea", SOLUTION)
        self.press("Run as Python")
        expect(p.locator(".panel h3")).to_have_text("2/2 passed", timeout=15000)
        p.fill("textarea", "print(0)")
        self.press("Run as Python")
        expect(p.locator(".panel h3")).to_have_text("0/2 passed", timeout=15000)
        expect(p.locator(".panel")).to_contain_text("first failure: ex1 WA")
        expect(p.locator(".panel pre")).to_contain_text("expected: 3")
        expect(p.locator(".panel")).to_contain_text("next review in")
        p.fill("textarea", "print(1/0)")
        self.press("Run as Python")
        expect(p.locator(".panel pre").first).to_contain_text("ZeroDivisionError", timeout=15000)
        self.no_overflow()

    def test_runner_has_no_network(self):
        p = self.page
        self.start("problems")
        p.fill("textarea", "import js\nprint(js.fetch('/api/export'))")
        self.press("Run as Python")
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
            self.press("Grade myself")
            expect(p.locator("h2")).to_have_text("Check yourself")
            p.locator("input[type=checkbox]").first.check()
            self.no_overflow()
            self.press("Save")
            expect(p.locator("body")).to_contain_text("saved on this device")
            if i == 0:
                p.get_by_role("button", name="Next", exact=True).click()
        self.press("Finish")
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
        self.press("Grade myself")
        p.locator("input[type=checkbox]").first.check()
        self.press("Save")
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
        self.press("Grade myself")
        expect(p.locator("h2")).to_have_text("Check yourself")
        boxes = p.locator("input[type=checkbox]")
        self.assertGreaterEqual(boxes.count(), 2)
        boxes.nth(0).check()
        boxes.nth(1).check()
        self.no_overflow()
        self.press("Save")
        expect(p.locator("h2")).to_contain_text("/ 5")
        self.press("Finish")
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
        self.press("Grade myself")
        expect(p.locator("h2")).to_have_text("Check yourself")


class MockInterviewTest(UiCase):
    def open_mock(self, own=None, lines=None):
        p = self.page
        p.click("nav button[data-tab=mock]")
        expect(p.locator("h1")).to_have_text("Mock interview")
        if own:
            p.fill("textarea", own)
        if lines:
            p.fill("input[type=number]", str(lines))
        self.press("Start")

    def answer(self, text, expect_line):
        self.page.fill("textarea", text)
        self.page.click("button.primary")
        expect(self.page.locator(".chat")).to_contain_text(expect_line)

    def test_full_interview_scores_and_history(self):
        p = self.page
        self.open_mock(lines=2)
        expect(p.locator(".chat")).to_contain_text("Probe 1?")
        self.answer("Start with requirements and 1M viewers", "Probe 2?")
        expect(p.locator("textarea")).to_have_attribute("placeholder", re.compile("final question"))
        self.no_overflow()
        p.fill("textarea", "Segments of 2 s behind an origin shield")
        p.click("button.primary")
        expect(p.locator("h1")).to_have_text("Review · 4.0")
        expect(p.locator("table tr")).to_have_count(6)
        expect(p.locator("body")).to_contain_text("size the origin")
        self.no_overflow()
        self.press("Done")
        expect(p.locator("body")).to_contain_text("· 4.0 · Design a live-streaming platform")
        p.click("nav button[data-tab=report]")
        expect(p.locator("body")).to_contain_text("Mock interviews")
        expect(p.locator("body")).to_contain_text("1 session · mean 4.0 · weakest: requirements")
        self.no_overflow()

    def test_report_shows_the_score_trend(self):
        p = self.page
        for i in range(2):
            if i == 0:
                self.open_mock(lines=2)
            else:  # "Done" shows the home; a second navigation would redraw it under the form
                p.fill("input[type=number]", "2")
                self.press("Start")
            self.answer("Start with requirements", "Probe 2?")
            p.fill("textarea", "Segments behind an origin shield")
            p.click("button.primary")
            expect(p.locator("h1")).to_have_text("Review · 4.0")
            self.press("Done")
            expect(p.locator("h1")).to_have_text("Mock interview")
        p.click("nav button[data-tab=report]")
        expect(p.locator("body")).to_contain_text("Mock score trend")
        expect(p.locator("body")).to_contain_text("0.0 (latest 1 vs the 1 before)")
        expect(p.locator("progress")).to_have_count(2)  # one bar per session
        self.no_overflow()

    def test_own_problem_and_finishing_early(self):
        p = self.page
        self.open_mock(own="Design a thumbnail service")
        expect(p.locator(".chat")).to_contain_text("Probe 1?")
        self.press("Finish and review")
        expect(p.locator(".err")).to_have_text("Answer at least once first.")
        self.answer("Generate them at upload", "Probe 2?")
        p.fill("textarea", "unsent draft counts too")
        self.press("Finish and review")
        expect(p.locator("h1")).to_contain_text("Review")
        self.press("Done")
        expect(p.locator("body")).to_contain_text("Design a thumbnail service")


class MockFailureTest(UiCase):
    server_env = {"UI_AI_FAIL": "1"}

    def test_a_failing_model_is_reported_and_can_be_retried(self):
        p = self.page
        p.click("nav button[data-tab=mock]")
        self.press("Start")
        expect(p.locator(".err")).to_contain_text("grader unavailable")
        expect(p.locator(".err")).to_contain_text("Press Send to retry")
        expect(p.locator("button.primary")).to_be_enabled()
        self.no_overflow()
        self.problems.clear()  # the 502 is logged by the browser


class MockWithoutGraderTest(UiCase):
    server_env = {"UI_LLM": "none"}

    def test_explains_that_it_needs_a_model(self):
        self.page.click("nav button[data-tab=mock]")
        expect(self.page.locator("body")).to_contain_text("Needs a model")
        expect(self.page.locator("button.primary")).to_have_count(0)


@unittest.skipUnless((WEB / "node_modules" / ".bin" / "wrangler").exists(), "needs npm install in web/")
class WorkerdTest(UiCase):
    """The same browser against the real runtime: real asset layer, _headers, D1, service worker."""
    launch = staticmethod(workerd)
    cards = TOTAL - COUNTS["problems"]  # no stand-in problem here

    def test_real_headers_and_a_self_graded_card(self):
        p = self.page
        expect(p.locator("h1")).to_have_text(f"{self.cards} due")
        expect(p.locator("body")).to_contain_text("You grade yourself")
        res = p.request.get(self.base + "/")
        self.assertIn("default-src 'none'", res.headers["content-security-policy"])
        self.assertEqual(res.headers["x-content-type-options"], "nosniff")
        self.assertEqual(p.request.get(self.base + "/api/status").json()["user"], "me")
        self.start("go")
        p.fill("textarea", "my answer")
        self.press("Grade myself")
        expect(p.locator("h2")).to_have_text("Check yourself")
        p.locator("input[type=checkbox]").first.check()
        self.press("Save")
        expect(p.locator("h2")).to_contain_text("/ 5")
        self.press("Finish")
        expect(p.locator("body")).to_contain_text("1 answered today")
        p.click("nav button[data-tab=report]")
        expect(p.locator("table")).to_contain_text("go")
        out = self.exported()
        self.assertEqual([a["answer"] for a in out["attempts"]], ["my answer"])
        self.assertEqual(len(out["card_state"]), 1)
        self.no_overflow()

    def test_offline_answer_is_queued_and_synced(self):
        p = self.page
        self.go_offline()
        expect(p.locator("h1")).to_have_text(f"Offline · {len(self.queue('drill.cards'))} cards")
        self.start("", 1)
        p.fill("textarea", "offline answer")
        self.press("Grade myself")
        expect(p.locator("h2")).to_have_text("Check yourself")
        p.locator("input[type=checkbox]").first.check()
        self.press("Save")
        expect(p.locator("body")).to_contain_text("saved on this device")
        self.press("Finish")
        self.assertEqual([q["answer"] for q in self.queue()], ["offline answer"])
        self.problems.clear()
        self.ctx.set_offline(False)  # the browser fires "online": the queue is sent by itself
        self.wait_until("JSON.parse(localStorage.getItem('drill.queue') || '[]').length === 0")
        self.assertEqual([a["answer"] for a in self.exported()["attempts"]], ["offline answer"])


class IconsTest(UiCase):
    """The PNG icons iOS and Android need are built from icon.svg (`npm run icons`)."""

    @classmethod
    def setUpClass(cls):
        if not (WEB / "node_modules" / "@resvg").exists():
            raise unittest.SkipTest("run `npm install` in web/ for the icon test")
        subprocess.run(["node", "scripts/make-icons.mjs"], cwd=WEB, check=True, capture_output=True)

    def test_home_screen_icons_are_served_as_png(self):
        p = self.page
        href = p.locator('link[rel="apple-touch-icon"]').get_attribute("href")
        manifest = p.request.get(self.base + "/manifest.webmanifest").json()
        pngs = [i for i in manifest["icons"] if i["type"] == "image/png"]
        self.assertEqual({i["purpose"] for i in pngs}, {"any", "maskable"})
        self.assertEqual({i["sizes"] for i in pngs}, {"192x192", "512x512"})
        for src in [href, *(i["src"] for i in pngs)]:
            r = p.request.get(self.base + src)
            self.assertEqual((src, r.status), (src, 200))
            self.assertEqual(r.headers["content-type"], "image/png")
            self.assertEqual(r.body()[1:4], b"PNG")


class RealPyodideTest(UiCase):
    """The real Pyodide runtime, vendored by `npm run vendor`, under the real CSP."""

    server_env = {"UI_PYODIDE": "real"}
    SLOW = 60_000

    @classmethod
    def setUpClass(cls):
        if not (WEB / "node_modules" / "pyodide").exists():
            raise unittest.SkipTest("run `npm install` in web/ for the real Pyodide tests")
        subprocess.run(["node", "scripts/vendor-pyodide.mjs"], cwd=WEB, check=True,
                       capture_output=True)

    def run_python(self, code):
        self.page.fill("textarea", code)
        self.press("Run as Python")

    def verdict(self, code, text):
        self.run_python(code)
        expect(self.page.locator(".panel")).to_contain_text(text, timeout=self.SLOW)

    def test_runs_correct_wrong_and_crashing_solutions(self):
        self.start("problems")
        self.run_python(SOLUTION)
        expect(self.page.locator(".panel h3")).to_have_text("2/2 passed", timeout=self.SLOW)
        self.verdict("print(0)", "first failure: ex1 WA")
        self.verdict("print(1 / 0)", "ZeroDivisionError")
        self.no_overflow()

    def test_stdlib_and_deep_recursion(self):
        self.start("problems")
        self.run_python(
            "import sys, heapq, bisect, collections, itertools, functools, re\n"
            "sys.setrecursionlimit(20000)\n"
            "def depth(n): return 0 if n == 0 else 1 + depth(n - 1)\n"
            "a, b = map(int, input().split())\n"
            "print(a + b + depth(5000) - 5000)")
        expect(self.page.locator(".panel h3")).to_have_text("2/2 passed", timeout=self.SLOW)

    def test_no_network_even_through_the_prototype_chain(self):
        # a blocked call must end in a runtime error (RE); an open one would print and give WA.
        # Pyodide sometimes reports the blocked call as "SystemError" instead of the Error text.
        self.start("problems")
        self.verdict("import js\nprint(js.fetch('/api/export'))", "first failure: ex1 RE")
        self.verdict("import js\nf = js.Object.getPrototypeOf(js.self).fetch\n"
                     "print(f.call(js.self, '/api/export'))", "first failure: ex1 RE")
        expect(self.page.locator(".panel h3")).to_have_text("0/2 passed")

    def test_eval_is_refused_in_the_real_runtime(self):
        self.start("problems")
        # The refusal reaches Python as the CSP message or, now and then, as a bare SystemError
        # (Pyodide), so only the verdict is exact: a runtime error, never a result.
        self.verdict("import js\njs.eval('1')", "first failure: ex1 RE")
        expect(self.page.locator(".panel h3")).to_have_text("0/2 passed")
        self.problems.clear()  # the browser may log the blocked eval as a console error

    def test_endless_loop_is_a_tle_and_the_runner_recovers(self):
        self.start("problems")
        self.verdict("while True: pass", "first failure: ex1 TLE")
        expect(self.page.locator(".panel h3")).to_have_text("0/2 passed")
        self.verdict(SOLUTION, "2/2 passed")


if __name__ == "__main__":
    unittest.main()

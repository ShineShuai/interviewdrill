import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import drill
import gen

CHUNK_A = (
    "Unbuffered channels synchronize sender and receiver: a send blocks until a receiver is "
    "ready. Buffered channels block a send only when the buffer is full, and block a receive "
    "only when it is empty. A goroutine blocked on a channel that nobody serves leaks forever."
)
GOOD = {
    "question": "How do buffered and unbuffered channels differ in Go?",
    "difficulty": 2,
    "points": [
        {"text": "Unbuffered sends wait for a receiver",
         "quote": "a send blocks until a receiver is ready"},
        {"text": "Buffered sends block only when full",
         "quote": "block a send only when the buffer is full"},
    ],
}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.cards = root / "cards"
        self.cards.mkdir()
        (self.cards / "go.md").write_text("## go-old | Old question about sched?\n- x\n")
        (root / "sources" / "go").mkdir(parents=True)
        (root / "sources" / "go" / "spec.txt").write_text(CHUNK_A + "\n\n" + "filler " * 40)
        for target, name, value in [
            (drill, "HOME", root / "home"), (drill, "CARDS", self.cards),
            (gen, "SOURCES", root / "sources"),
        ]:
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.db = gen.connect()

    def run_gen(self, cards, verdicts=None):
        verdicts = verdicts or (lambda text, claims: [True] * len(claims))
        return gen.generate(self.db, "go", 5, 5, lambda text, k: cards, verdicts)


class TextTest(unittest.TestCase):
    def test_quote_check(self):
        self.assertTrue(gen.quote_ok("A send  blocks\nuntil a RECEIVER is ready", CHUNK_A))
        self.assertFalse(gen.quote_ok("a send never blocks at all", CHUNK_A))
        self.assertFalse(gen.quote_ok("a send", CHUNK_A))  # too short to be evidence
        self.assertTrue(gen.quote_ok("it\u2019s fine \u2014 really", "It's fine - really"))

    def test_similar(self):
        self.assertTrue(gen.similar("How do buffered and unbuffered channels differ?",
                                    "Buffered vs unbuffered channels: how do they differ?"))
        self.assertFalse(gen.similar("What is RAII?", "Explain the Go scheduler"))

    def test_make_id_is_unique_and_clean(self):
        a = gen.make_id("go", "What is a goroutine leak? | really", set())
        self.assertEqual(a, "go-what-is-a-goroutine-leak")
        self.assertEqual(gen.make_id("go", "What is a goroutine leak?", {a}), a + "-2")

    def test_split_text(self):
        para = "word " * 100
        chunks = gen.split_text("\n\n".join([para] * 8) + "\n\ntiny", size=1000)
        self.assertTrue(all(200 <= len(c) <= 1500 for c in chunks))
        self.assertNotIn("tiny", chunks[-1][-10:] if len(chunks[-1]) < 200 else "")
        self.assertEqual(gen.split_text("short"), [])
        self.assertTrue(all(len(c) <= 1000 for c in gen.split_text("x" * 5000, size=1000)))

    def test_html_to_text(self):
        html = ("<html><head><title>t</title><style>p{}</style></head><body><nav>menu</nav>"
                "<h1>Title</h1><p>One &amp; two</p><script>evil()</script><ul><li>a</li><li>b</li>"
                "</ul></body></html>")
        self.assertEqual(gen.html_to_text(html), "Title\n\nOne & two\n\na\nb")

    def test_call_tool_request_shape(self):
        seen = {}

        def create(**kw):
            seen.update(kw)
            block = types.SimpleNamespace(type="tool_use", input={"ok": 1})
            return types.SimpleNamespace(content=[block])

        fake = types.SimpleNamespace(
            Anthropic=lambda: types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
        )
        with mock.patch.dict(sys.modules, {"anthropic": fake}):
            out = gen.call_tool("sys", gen.VERIFY_TOOL, "prompt")
        self.assertEqual(out, {"ok": 1})
        self.assertEqual(seen["tool_choice"], {"type": "tool", "name": "verdicts"})
        self.assertEqual(seen["messages"], [{"role": "user", "content": "prompt"}])


class PipelineTest(Base):
    INVENTED = {
        **GOOD,
        "question": "What does the Go memory model promise?",
        "points": [{"text": "a", "quote": "this sentence is not in the source"},
                   {"text": "b", "quote": "neither is this sentence at all"}],
    }

    def test_ungrounded_and_duplicate_cards_are_rejected(self):
        dup = {**GOOD, "question": "Old question about sched?"}
        stats = self.run_gen([GOOD, self.INVENTED, dup, GOOD])
        self.assertEqual(dict(stats), {
            "chunks": 1, "proposed": 4, "points_ungrounded": 2, "points_unsupported": 0,
            "rejected_ungrounded": 1, "rejected_duplicate": 2, "pending": 1,
        })

    def test_unsupported_points_are_dropped_not_the_card(self):
        third = {"text": "Buffered receives wait when empty",
                 "quote": "block a receive only when it is empty"}
        card = {**GOOD, "points": [*GOOD["points"], third]}
        stats = self.run_gen([card], lambda text, claims: [True, False, True])
        self.assertEqual((stats["pending"], stats["points_unsupported"]), (1, 1))
        points = self.db.execute("SELECT points FROM candidates").fetchone()[0]
        self.assertEqual([p["text"] for p in json.loads(points)],
                         ["Unbuffered sends wait for a receiver",
                          "Buffered receives wait when empty"])

    def test_too_few_supported_points_rejects_the_card(self):
        stats = self.run_gen([GOOD], lambda text, claims: [True, False])
        self.assertEqual((stats["rejected_unsupported"], stats["pending"]), (1, 0))

    def test_chunk_rotation_prefers_untried(self):
        self.run_gen([])
        tries = dict(self.db.execute("SELECT chunk_id, tries FROM chunk_tries").fetchall())
        self.assertEqual(list(tries.values()), [1] * len(tries))
        self.assertTrue(all(k.startswith("go/spec#") for k in tries))


class ReviewTest(Base):
    def approve_all(self, keys, edit=None):
        self.run_gen([GOOD])
        quiet = contextlib.redirect_stdout(io.StringIO())
        with mock.patch("builtins.input", side_effect=keys), quiet:
            return gen.review(self.db, edit_fn=edit or (lambda q, p: (q, p)))

    def test_approve_writes_card_with_provenance_and_keeps_old_cards(self):
        done = self.approve_all(["a"])
        self.assertEqual(dict(done), {"approved": 1})
        text = (self.cards / "go.md").read_text()
        old = "## go-old | Old question about sched?\n- x\n\n## go-how-do-"
        self.assertTrue(text.startswith(old))
        self.assertIn("<!-- src: go/spec#0; difficulty: 2; verified: quote+entailment -->", text)
        new = [c for c in drill.load_cards(self.cards) if c["id"] != "go-old"][0]
        self.assertEqual(new["ref"], "- Unbuffered sends wait for a receiver\n"
                                     "- Buffered sends block only when full")
        status = self.db.execute("SELECT status FROM candidates").fetchone()[0]
        self.assertEqual(status, "approved")

    def test_edit_then_approve_is_marked_human_edited(self):
        self.approve_all(["e", "a"], edit=lambda q, p: ("Edited Q?", ["only one", "two"]))
        text = (self.cards / "go.md").read_text()
        self.assertIn("## go-edited-q | Edited Q?", text)
        self.assertIn("verified: human-edited", text)

    def test_reject_skip_quit(self):
        self.assertEqual(dict(self.approve_all(["x", "r"])), {"rejected": 1})
        status = self.db.execute("SELECT status FROM candidates").fetchone()[0]
        self.assertEqual(status, "rejected")
        self.assertNotIn("go-how-do", (self.cards / "go.md").read_text())
        self.db.execute("UPDATE candidates SET status = 'pending'")
        with contextlib.redirect_stdout(io.StringIO()):
            with mock.patch("builtins.input", side_effect=["s"]):
                self.assertEqual(dict(gen.review(self.db)), {"skipped": 1})
            with mock.patch("builtins.input", side_effect=["q"]):
                self.assertEqual(dict(gen.review(self.db)), {})
        self.assertEqual(self.db.execute("SELECT status FROM candidates").fetchone()[0], "pending")


if __name__ == "__main__":
    unittest.main()

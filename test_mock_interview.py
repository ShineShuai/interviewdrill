import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import drill
import llm
import mock_interview as mockmod

REVIEW = {"scores": dict.fromkeys(mockmod.AXES, 3), "strengths": ["clear structure"],
          "gaps": ["no failure modes"], "next_steps": ["practise estimation"]}


class Model:
    """Scripted stand-in for llm.call_tool: records every call."""

    def __init__(self):
        self.calls = []

    def __call__(self, system, tool, prompt, max_tokens=2000):
        self.calls.append((tool["name"], prompt))
        if tool["name"] == "review":
            return REVIEW
        return {"say": f" question {sum(c[0] == 'interviewer' for c in self.calls)} "}


def answers(*texts):
    it = iter(texts)
    return lambda: next(it, "")


class InterviewTest(unittest.TestCase):
    def run_interview(self, n, *texts):
        model, out = Model(), []
        with mock.patch.object(llm, "call_tool", model):
            turns = mockmod.interview("P", n, read=answers(*texts), out=out.append)
        return turns, model, out

    def test_alternates_and_ends_on_an_interviewer_line(self):
        turns, model, _ = self.run_interview(3, "a1", "a2")
        self.assertEqual([t["who"] for t in turns],
                         ["interviewer", "candidate", "interviewer", "candidate", "interviewer"])
        self.assertEqual(turns[0]["text"], "question 1")  # stripped
        self.assertEqual(len(model.calls), 3)

    def test_each_question_sees_the_earlier_answers_and_the_last_is_flagged(self):
        _, model, _ = self.run_interview(3, "my first answer", "second one")
        self.assertIn("(nothing yet)", model.calls[0][1])
        self.assertIn("Candidate: my first answer", model.calls[1][1])
        self.assertIn("Candidate: second one", model.calls[2][1])
        self.assertIn("line 3 of 3", model.calls[2][1])
        self.assertIn("last: ask one final", model.calls[2][1])
        self.assertNotIn("last: ask one final", model.calls[1][1])

    def test_an_empty_answer_ends_the_interview(self):
        turns, model, _ = self.run_interview(5, "only answer")
        self.assertEqual(len(turns), 3)
        self.assertEqual(len(model.calls), 2)

    def test_review_gets_the_whole_transcript(self):
        turns, _, _ = self.run_interview(2, "a1")
        model = Model()
        with mock.patch.object(llm, "call_tool", model):
            self.assertEqual(mockmod.review("P", turns), REVIEW)
        self.assertIn("Candidate: a1", model.calls[0][1])

    def test_review_schema_rejects_a_missing_axis(self):
        bad = {**REVIEW, "scores": {"requirements": 3}}
        self.assertTrue(llm.check(bad, mockmod.REVIEW_TOOL["input_schema"]))
        self.assertFalse(llm.check(REVIEW, mockmod.REVIEW_TOOL["input_schema"]))
        too_high = {**REVIEW, "scores": dict.fromkeys(mockmod.AXES, 6)}
        self.assertTrue(llm.check(too_high, mockmod.REVIEW_TOOL["input_schema"]))


class CliTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        for p in (mock.patch.object(drill, "HOME", self.home),
                  mock.patch.object(llm, "call_tool", Model()),
                  mock.patch.object(llm, "describe", lambda: "test")):
            p.start()
            self.addCleanup(p.stop)

    def main(self, argv, stdin=""):
        out = io.StringIO()
        with mock.patch("sys.argv", ["mock_interview.py", *argv]), mock.patch("sys.stdin", io.StringIO(stdin)), \
                contextlib.redirect_stdout(out):
            mockmod.main()
        return out.getvalue()

    def test_full_session_prints_scores_and_saves_a_line(self):
        text = self.main(["-n", "2", "-s", "2"], "I would start with requirements\n\n")
        self.assertIn("Interviewer: question 1", text)
        self.assertIn("mean 3.0", text)
        self.assertIn("  - no failure modes", text)
        (row,) = [json.loads(ln) for ln in (self.home / "mock.jsonl").read_text().splitlines()]
        self.assertEqual(row["problem"], mockmod.SCENARIOS[1])
        self.assertEqual(row["review"], REVIEW)
        self.assertEqual([t["who"] for t in row["turns"]],
                         ["interviewer", "candidate", "interviewer"])

    def test_own_problem_and_list(self):
        self.main(["-n", "2", "-p", "Design a thumbnail service"], "answer\n\n")
        row = json.loads((self.home / "mock.jsonl").read_text())
        self.assertEqual(row["problem"], "Design a thumbnail service")
        self.assertEqual(self.main(["--list"]).count("\n"), len(mockmod.SCENARIOS))

    def test_no_answer_means_no_review_and_no_file(self):
        with self.assertRaises(SystemExit) as cm:
            self.main(["-n", "3", "-s", "1"], "")
        self.assertIn("no answer given", str(cm.exception))
        self.assertFalse((self.home / "mock.jsonl").exists())

    def test_without_a_model_it_says_so(self):
        def refuse(*a, **k):
            raise llm.LLMError("no model configured")
        with mock.patch.object(llm, "call_tool", refuse), self.assertRaises(SystemExit) as cm:
            self.main(["-n", "2", "-s", "1"], "x\n\n")
        self.assertIn("needs a model", str(cm.exception))

    def test_bad_arguments(self):
        for argv in (["-n", "1"], ["-n", "13"], ["-s", "0"], ["-s", "99"]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                self.main(argv)


if __name__ == "__main__":
    unittest.main()

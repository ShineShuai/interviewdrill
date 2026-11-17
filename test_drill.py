import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import drill
import llm


class ScheduleTest(unittest.TestCase):
    def test_fail_resets_interval(self):
        ease, interval, reps = drill.schedule(2.5, 10.0, 4, 2.0)
        self.assertEqual((interval, reps), (1.0, 0))
        self.assertLess(ease, 2.5)

    def test_progression(self):
        e, i, r = drill.schedule(2.5, 0.0, 0, 5)
        self.assertEqual((i, r), (1.0, 1))
        e, i, r = drill.schedule(e, i, r, 5)
        self.assertEqual((i, r), (3.0, 2))
        _, i, _ = drill.schedule(e, i, r, 5)
        self.assertGreater(i, 3.0)


class JudgeHintTest(unittest.TestCase):
    def test_exercise_kinds_get_a_hint(self):
        plain = drill.system_for("go-chan")
        self.assertEqual(plain, drill.SYSTEM)
        self.assertIn("debugging exercise", drill.system_for("bug:uaf"))
        self.assertIn("coding exercise", drill.system_for("problem:max-sum"))
        self.assertTrue(drill.system_for("bug:x").startswith(drill.SYSTEM))
        self.assertEqual(drill.system_for("bugfix"), drill.SYSTEM)


class SelfGradeTest(unittest.TestCase):
    CARD = {"id": "go-chan", "question": "Q?",
            "ref": "- unbuffered blocks\n- buffered fills\n- leaks"}

    def grade(self, typed, card=None):
        out = io.StringIO()
        with mock.patch("builtins.input", return_value=typed), contextlib.redirect_stdout(out):
            return drill.self_grade(card or self.CARD), out.getvalue()

    def test_checklist_per_kind(self):
        self.assertEqual(drill.checklist("go-chan", "- a\n- b\ntext\n-c"), ["a", "b"])
        self.assertEqual(drill.checklist("go-x", "no bullets"), ["no bullets"])
        self.assertEqual(len(drill.checklist("bug:x", "- Bug class: x\n-    int* p;")), 3)
        self.assertEqual(len(drill.checklist("problem:x", "- Intended approach: y")), 4)

    def test_self_grade_scores_the_ticked_share(self):
        g, shown = self.grade("1 3")
        self.assertEqual((g["correct"], g["depth"], g["clarity"]), (3, 3, 3))  # round(5 * 2/3)
        self.assertEqual(g["missed"], ["buffered fills"])
        self.assertIn("1. unbuffered blocks", shown)
        self.assertIn("Reference:", shown)
        self.assertEqual(drill.score(g), 3)

    def test_all_none_and_garbage(self):
        self.assertEqual(self.grade("all")[0]["correct"], 5)
        self.assertEqual(self.grade("none")[0]["correct"], 0)
        self.assertEqual(self.grade("")[0]["missed"],
                         ["unbuffered blocks", "buffered fills", "leaks"])
        self.assertEqual(self.grade("9 0 x -1")[0]["correct"], 0)  # out of range ignored
        self.assertEqual(self.grade("2,2,2")[0]["correct"], 2)  # duplicates count once

    def test_exercise_cards_use_the_fixed_checklist(self):
        card = {"id": "bug:x", "question": "Find it", "ref": "- Bug class: uaf\n-    int* p;"}
        g, shown = self.grade("1 2 3", card)
        self.assertEqual(g["correct"], 5)
        self.assertIn("3. Gave a fix that works", shown)


class GradeRoutingTest(unittest.TestCase):
    CARD = {"id": "go-chan", "question": "Q?", "ref": "- a"}
    MODEL_GRADE = {"correct": 5, "depth": 5, "clarity": 5, "missed": [], "errors": [],
                   "followup": ""}

    def route(self, env, judge=None):
        keep = {k: v for k, v in os.environ.items()
                if not k.startswith(("DRILL_LLM", "ANTHROPIC"))}
        patches = [mock.patch.dict(os.environ, {**keep, **env}, clear=True),
                   mock.patch.object(drill, "self_grade", return_value="SELF"),
                   contextlib.redirect_stdout(io.StringIO())]
        if judge is not None:
            patches.append(mock.patch.object(drill, "judge", judge))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            return drill.grade(self.CARD, "my answer")

    def test_no_configuration_means_self_grading(self):
        self.assertEqual(self.route({}), "SELF")
        self.assertEqual(self.route({"DRILL_LLM": "none"}), "SELF")

    def test_a_configured_model_grades(self):
        calls = []

        def judge(card, answer):
            calls.append((card["id"], answer))
            return self.MODEL_GRADE

        self.assertEqual(self.route({"ANTHROPIC_API_KEY": "k"}, judge), self.MODEL_GRADE)
        self.assertEqual(self.route({"DRILL_LLM_URL": "http://x"}, judge), self.MODEL_GRADE)
        self.assertEqual(self.route({"DRILL_LLM": "manual"}, judge), self.MODEL_GRADE)
        self.assertEqual(calls, [("go-chan", "my answer")] * 3)

    def test_a_failing_model_falls_back_to_self_grading(self):
        def broken(card, answer):
            raise llm.LLMError("HTTP 429: quota")

        self.assertEqual(self.route({"ANTHROPIC_API_KEY": "k"}, broken), "SELF")

    def test_a_bad_setting_falls_back_too(self):
        self.assertEqual(self.route({"DRILL_LLM": "nonsense"}), "SELF")


class JudgeTest(unittest.TestCase):
    def test_judge_sends_the_kind_hint_and_cleans_the_reply(self):
        seen = {}

        def fake(system, tool, prompt, max_tokens=2000):
            seen.update(system=system, tool=tool["name"], prompt=prompt, max_tokens=max_tokens)
            return {"correct": "9", "depth": -3, "clarity": 3.6, "missed": ["m", 5],
                    "errors": [{"quote": "q"}, "junk", None], "followup": "f" * 900}

        with mock.patch.object(llm, "call_tool", fake):
            g = drill.judge({"id": "bug:x", "question": "Q", "ref": "R"}, "")
        self.assertIn("debugging exercise", seen["system"])
        self.assertIn("Candidate answer:\n(no answer)", seen["prompt"])
        self.assertEqual((seen["tool"], seen["max_tokens"]), ("grade", 1000))
        self.assertEqual((g["correct"], g["depth"], g["clarity"]), (5, 0, 4))
        self.assertEqual(g["missed"], ["m", "5"])
        self.assertEqual(g["errors"], [{"quote": "q", "why": ""}])
        self.assertEqual(len(g["followup"]), 500)

    def test_clean_grade_survives_garbage(self):
        g = drill.clean_grade({"correct": None, "depth": "x", "missed": None, "errors": None})
        self.assertEqual((g["correct"], g["depth"], g["clarity"], g["missed"], g["errors"]),
                         (0, 0, 0, [], []))


class CardsTest(unittest.TestCase):
    def test_cards_parse(self):
        cards = drill.load_cards()
        ids = [c["id"] for c in cards]
        self.assertTrue(cards)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(c["question"] and c["ref"] for c in cards))

    def test_seed_cards_have_a_usable_checklist(self):
        cards = drill.load_cards()
        for c in cards:
            bullets = [ln for ln in c["ref"].splitlines() if ln.startswith("- ")]
            self.assertTrue(3 <= len(bullets) <= 6, c["id"])
        self.assertGreaterEqual(sum(c["topic"] == "video" for c in cards), 20)

    def test_provenance_comment_is_not_reference(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "t.md").write_text("## t-a | Q?\n<!-- src: x -->\n- p\n")
            (card,) = drill.load_cards(Path(d))
        self.assertEqual((card["id"], card["question"], card["ref"]), ("t-a", "Q?", "- p"))


if __name__ == "__main__":
    unittest.main()

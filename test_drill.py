import tempfile
import unittest
from pathlib import Path

import drill


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
        self.assertIn("coding exercise", drill.system_for("kata:max-sum"))
        self.assertTrue(drill.system_for("bug:x").startswith(drill.SYSTEM))
        self.assertEqual(drill.system_for("bugfix"), drill.SYSTEM)


class CardsTest(unittest.TestCase):
    def test_cards_parse(self):
        cards = drill.load_cards()
        ids = [c["id"] for c in cards]
        self.assertTrue(cards)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(c["question"] and c["ref"] for c in cards))

    def test_provenance_comment_is_not_reference(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "t.md").write_text("## t-a | Q?\n<!-- src: x -->\n- p\n")
            (card,) = drill.load_cards(Path(d))
        self.assertEqual((card["id"], card["question"], card["ref"]), ("t-a", "Q?", "- p"))


if __name__ == "__main__":
    unittest.main()

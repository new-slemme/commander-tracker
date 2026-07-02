import unittest

import app


class CommanderBracketScoreMappingTests(unittest.TestCase):
    """Pin the score -> bracket mapping to the CLAUDE.md-documented thresholds:
    0 -> 1, 1-2 -> 2, 3-4 -> 3, 5-7 -> 4, 8+ -> 5.
    """

    def test_score_to_bracket_pairs(self):
        cases = {
            0: 1,
            1: 2,
            2: 2,
            3: 3,
            4: 3,
            5: 4,
            7: 4,
            8: 5,
            10: 5,
        }
        for score, expected_bracket in cases.items():
            with self.subTest(score=score):
                self.assertEqual(app._commander_bracket_for_score(score), expected_bracket)


if __name__ == "__main__":
    unittest.main()
